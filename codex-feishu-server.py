"""
Feishu + Codex CLI integration service.

Receives development requirements from Feishu messages,
invokes Codex CLI to execute them,
and sends execution logs back to Feishu.

Usage:
    uv run python codex-feishu-server.py [--work-dir /path/to/project] [--codex-bin /path/to/codex] [--session-store /path/to/session-store.json]
"""

import argparse
import asyncio
import importlib.util
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config import APP_ID, APP_SECRET


def _load_claude_service_module():
    script_dir = Path(__file__).resolve().parent
    service_path = script_dir / "claude-feishu-service.py"
    spec = importlib.util.spec_from_file_location("claude_feishu_service", service_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load Feishu service module: {service_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_claude_service_module = _load_claude_service_module()
FeishuBot = _claude_service_module.FeishuBot


@dataclass
class CodexRunResult:
    output: str
    thread_id: str | None
    success: bool


class SessionStore:
    """Persist Feishu chat -> Codex thread mappings."""

    def __init__(self, store_path: str, work_dir: str):
        self.store_path = Path(store_path).expanduser().resolve()
        self.work_dir = os.path.abspath(work_dir)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return {"workspaces": {}}

        try:
            with open(self.store_path, "r", encoding="utf-8") as file:
                data = json.load(file)
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            logger.warning("Failed to load session store {}: {}", self.store_path, exc)

        return {"workspaces": {}}

    def _save(self) -> None:
        temp_path = self.store_path.with_suffix(f"{self.store_path.suffix}.tmp")
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(self._data, file, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temp_path, self.store_path)

    def _workspace_sessions(self) -> dict[str, dict[str, Any]]:
        workspaces = self._data.setdefault("workspaces", {})
        if not isinstance(workspaces, dict):
            self._data["workspaces"] = {}
            workspaces = self._data["workspaces"]

        sessions = workspaces.setdefault(self.work_dir, {})
        if not isinstance(sessions, dict):
            workspaces[self.work_dir] = {}
            sessions = workspaces[self.work_dir]
        return sessions

    def get(self, chat_id: str) -> str | None:
        session = self._workspace_sessions().get(chat_id)
        if isinstance(session, dict):
            thread_id = session.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                return thread_id
        return None

    def set(self, chat_id: str, thread_id: str) -> None:
        self._workspace_sessions()[chat_id] = {
            "thread_id": thread_id,
            "updated_at": int(time.time()),
        }
        self._save()

    def clear(self, chat_id: str) -> bool:
        removed = self._workspace_sessions().pop(chat_id, None) is not None
        if removed:
            self._save()
        return removed

    def count(self) -> int:
        return len(self._workspace_sessions())


class CodexRunner:
    """Run Codex CLI and capture the final response and thread id."""

    def __init__(self, work_dir: str, codex_bin: str):
        self.work_dir = work_dir
        self.codex_bin = codex_bin

    async def run(self, prompt: str, thread_id: str | None = None) -> CodexRunResult:
        output_path = None
        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                prefix="codex-last-message-",
                suffix=".txt",
            ) as temp_file:
                output_path = temp_file.name

            cmd = self._build_cmd(prompt, output_path, thread_id)
            logger.info(
                "Running Codex [{}]: {}",
                f"resume:{thread_id}" if thread_id else "new-session",
                prompt,
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.work_dir,
                env=env,
            )

            stdout, stderr = await proc.communicate()
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            events = self._parse_json_events(stdout_text)
            resolved_thread_id = self._extract_thread_id(events) or thread_id

            result = ""
            if output_path and os.path.exists(output_path):
                with open(output_path, "r", encoding="utf-8", errors="replace") as file:
                    result = file.read().strip()
            if not result:
                result = self._extract_last_agent_message(events)

            if proc.returncode != 0:
                parts = [f"**Exit code:** {proc.returncode}"]
                if resolved_thread_id:
                    parts.append(f"**Session:** `{resolved_thread_id}`")
                if result:
                    parts.append(f"**Last message:**\n```\n{result}\n```")
                if stderr_text:
                    parts.append(f"**Stderr:**\n```\n{stderr_text}\n```")
                if stdout_text:
                    parts.append(f"**JSON events:**\n```\n{stdout_text}\n```")
                return CodexRunResult(
                    output="\n\n".join(parts).strip(),
                    thread_id=resolved_thread_id,
                    success=False,
                )

            if resolved_thread_id and result:
                result = f"**Session:** `{resolved_thread_id}`\n\n{result}"
            elif resolved_thread_id:
                result = f"**Session:** `{resolved_thread_id}`"

            if result:
                return CodexRunResult(output=result, thread_id=resolved_thread_id, success=True)
            if stderr_text:
                return CodexRunResult(
                    output=f"**Codex stderr:**\n```\n{stderr_text}\n```",
                    thread_id=resolved_thread_id,
                    success=True,
                )
            return CodexRunResult(output="(no output)", thread_id=resolved_thread_id, success=True)

        except FileNotFoundError as exc:
            return CodexRunResult(
                output=f"Error: `{self.codex_bin}` command not found. Make sure Codex CLI is installed and available. Details: {exc}",
                thread_id=thread_id,
                success=False,
            )
        except Exception as exc:
            return CodexRunResult(
                output=f"Error running Codex: {exc}",
                thread_id=thread_id,
                success=False,
            )
        finally:
            if output_path and os.path.exists(output_path):
                os.remove(output_path)

    def _build_cmd(self, prompt: str, output_path: str, thread_id: str | None) -> list[str]:
        common_args = [
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-o",
            output_path,
        ]
        if thread_id:
            return [
                self.codex_bin,
                "exec",
                "resume",
                *common_args,
                thread_id,
                prompt,
            ]
        return [
            self.codex_bin,
            "exec",
            *common_args,
            prompt,
        ]

    @staticmethod
    def _parse_json_events(stdout_text: str) -> list[dict[str, Any]]:
        events = []
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                events.append(data)
        return events

    @staticmethod
    def _extract_thread_id(events: list[dict[str, Any]]) -> str | None:
        for event in events:
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id.strip():
                    return thread_id
        return None

    @staticmethod
    def _extract_last_agent_message(events: list[dict[str, Any]]) -> str:
        last_message = ""
        for event in events:
            if event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    last_message = text.strip()
        return last_message


class CodexFeishuService:
    """Bridges Feishu messages to Codex CLI execution."""

    HELP_TEXT = """**Codex Feishu Bot**

Send me a development task and I'll use Codex to work on it.

**Commands:**
- Send any text -> Continue the current chat's Codex session
- `/status` -> Check service and current chat session status
- `/reset` or `/new` -> Start a fresh Codex session for this chat
- `/help` -> Show this help message

**Session behavior:**
- The same Feishu chat/group continues the same Codex context
- Different Feishu chats keep different Codex sessions
- After `/reset`, the next message starts a brand-new Codex session

**Tips:**
- Be specific about what you want (e.g., "Add a login page to src/app.py")
- The working directory is: `{work_dir}`
"""

    def __init__(self, work_dir: str, codex_bin: str, session_store_path: str):
        self.bot = FeishuBot(APP_ID, APP_SECRET)
        self.runner = CodexRunner(work_dir, codex_bin)
        self.sessions = SessionStore(session_store_path, work_dir)
        self.work_dir = work_dir
        self.codex_bin = codex_bin
        self.session_store_path = str(Path(session_store_path).expanduser().resolve())
        self._active_tasks: dict[str, bool] = {}

        self.bot.on_message(self._handle_message)

    async def start(self):
        logger.info("Codex Feishu Service starting...")
        logger.info("Working directory: {}", self.work_dir)
        logger.info("Codex binary: {}", self.codex_bin)
        logger.info("Session store: {}", self.session_store_path)
        await self.bot.start()

    async def _handle_message(self, sender_id: str, chat_id: str, chat_type: str, text: str):
        command = text.strip().lower()

        if command == "/help":
            await self.bot.send_card(
                chat_id,
                "Help",
                self.HELP_TEXT.format(work_dir=self.work_dir),
            )
            return

        if command == "/status":
            active = [cid for cid, running in self._active_tasks.items() if running]
            status = "idle" if not active else f"running ({len(active)} task(s))"
            current_thread_id = self.sessions.get(chat_id)
            session_status = f"`{current_thread_id}`" if current_thread_id else "none"
            await self.bot.send_card(
                chat_id,
                "Status",
                "\n".join(
                    [
                        f"**Service status:** {status}",
                        f"**Current chat session:** {session_status}",
                        f"**Saved sessions in this workspace:** {self.sessions.count()}",
                        f"**Working directory:** `{self.work_dir}`",
                        f"**Codex binary:** `{self.codex_bin}`",
                        f"**Session store:** `{self.session_store_path}`",
                    ]
                ),
            )
            return

        if command in {"/reset", "/new"}:
            if self._active_tasks.get(chat_id):
                await self.bot.send_text(
                    chat_id,
                    "A task is already running for this chat. Please wait for it to finish before resetting the session.",
                )
                return

            removed = self.sessions.clear(chat_id)
            message = "Current chat session cleared. Your next message will start a fresh Codex session."
            if not removed:
                message = "No existing Codex session was found for this chat. Your next message will start a fresh session."
            await self.bot.send_card(chat_id, "Session Reset", message)
            return

        if self._active_tasks.get(chat_id):
            await self.bot.send_text(
                chat_id,
                "A task is already running for this chat. Please wait for it to finish.",
            )
            return

        self._active_tasks[chat_id] = True
        existing_thread_id = self.sessions.get(chat_id)
        mode = "resume" if existing_thread_id else "new"
        session_line = f"**Session:** `{existing_thread_id}`\n" if existing_thread_id else "**Session:** new\n"

        await self.bot.send_card(
            chat_id,
            "Task Accepted",
            f"**Mode:** {mode}\n{session_line}\n**Prompt:**\n```\n{text}\n```\n\nCodex is working on it...",
        )

        try:
            result = await self.runner.run(text, existing_thread_id)
            if result.thread_id:
                self.sessions.set(chat_id, result.thread_id)

            title = "Task Completed" if result.success else "Task Failed"
            await self.bot.send_card(chat_id, title, result.output)
        except Exception as exc:
            logger.error("Task error: {}", exc)
            await self.bot.send_card(
                chat_id,
                "Task Failed",
                f"**Error:**\n```\n{exc}\n```",
            )
        finally:
            self._active_tasks[chat_id] = False


def main():
    parser = argparse.ArgumentParser(description="Feishu + Codex CLI integration service")
    parser.add_argument(
        "--work-dir",
        default=os.getcwd(),
        help="Working directory for Codex (default: current directory)",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Path to the Codex CLI binary (default: codex)",
    )
    parser.add_argument(
        "--session-store",
        default=str(Path(__file__).resolve().with_name(".codex-feishu-sessions.json")),
        help="Path to the persisted Feishu chat -> Codex session mapping file",
    )
    args = parser.parse_args()

    work_dir = os.path.abspath(args.work_dir)
    if not os.path.isdir(work_dir):
        logger.error("Working directory does not exist: {}", work_dir)
        return

    service = CodexFeishuService(
        work_dir=work_dir,
        codex_bin=args.codex_bin,
        session_store_path=args.session_store,
    )

    try:
        asyncio.run(service.start())
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()
