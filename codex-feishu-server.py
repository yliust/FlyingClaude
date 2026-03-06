"""
Feishu + Codex CLI integration service.

Receives development requirements from Feishu messages,
invokes Codex CLI to execute them,
and sends execution logs back to Feishu.

Usage:
    uv run python codex-feishu-server.py [--work-dir /path/to/project] [--codex-bin /path/to/codex]
"""

import argparse
import asyncio
import importlib.util
import os
import tempfile
from pathlib import Path

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


class CodexRunner:
    """Run Codex CLI and capture the final response."""

    def __init__(self, work_dir: str, codex_bin: str):
        self.work_dir = work_dir
        self.codex_bin = codex_bin

    async def run(self, prompt: str) -> str:
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

            cmd = [
                self.codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--dangerously-bypass-approvals-and-sandbox",
                "-o",
                output_path,
                prompt,
            ]

            logger.info("Running Codex: {}", prompt)

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

            result = ""
            if output_path and os.path.exists(output_path):
                with open(output_path, "r", encoding="utf-8", errors="replace") as file:
                    result = file.read().strip()

            if proc.returncode != 0:
                parts = [f"**Exit code:** {proc.returncode}"]
                if result:
                    parts.append(f"**Last message:**\n```\n{result}\n```")
                if stdout_text:
                    parts.append(f"**Stdout:**\n```\n{stdout_text}\n```")
                if stderr_text:
                    parts.append(f"**Stderr:**\n```\n{stderr_text}\n```")
                return "\n\n".join(parts).strip()

            if result:
                return result
            if stdout_text:
                return stdout_text
            if stderr_text:
                return f"**Codex stderr:**\n```\n{stderr_text}\n```"
            return "(no output)"

        except FileNotFoundError as exc:
            return f"Error: `{self.codex_bin}` command not found. Make sure Codex CLI is installed and available. Details: {exc}"
        except Exception as exc:
            return f"Error running Codex: {exc}"
        finally:
            if output_path and os.path.exists(output_path):
                os.remove(output_path)


class CodexFeishuService:
    """Bridges Feishu messages to Codex CLI execution."""

    HELP_TEXT = """**Codex Feishu Bot**

Send me a development task and I'll use Codex to work on it.

**Commands:**
- Send any text -> Execute as Codex prompt
- `/status` -> Check if the service is running
- `/help` -> Show this help message

**Tips:**
- Be specific about what you want (e.g., "Add a login page to src/app.py")
- The working directory is: `{work_dir}`
"""

    def __init__(self, work_dir: str, codex_bin: str):
        self.bot = FeishuBot(APP_ID, APP_SECRET)
        self.runner = CodexRunner(work_dir, codex_bin)
        self.work_dir = work_dir
        self.codex_bin = codex_bin
        self._active_tasks: dict[str, bool] = {}

        self.bot.on_message(self._handle_message)

    async def start(self):
        logger.info("Codex Feishu Service starting...")
        logger.info("Working directory: {}", self.work_dir)
        logger.info("Codex binary: {}", self.codex_bin)
        await self.bot.start()

    async def _handle_message(self, sender_id: str, chat_id: str, chat_type: str, text: str):
        if text.lower() == "/help":
            await self.bot.send_card(
                chat_id,
                "Help",
                self.HELP_TEXT.format(work_dir=self.work_dir),
            )
            return

        if text.lower() == "/status":
            active = [cid for cid, running in self._active_tasks.items() if running]
            status = "idle" if not active else f"running ({len(active)} task(s))"
            await self.bot.send_card(
                chat_id,
                "Status",
                f"**Service status:** {status}\n**Working directory:** `{self.work_dir}`\n**Codex binary:** `{self.codex_bin}`",
            )
            return

        if self._active_tasks.get(chat_id):
            await self.bot.send_text(
                chat_id,
                "A task is already running for this chat. Please wait for it to finish.",
            )
            return

        self._active_tasks[chat_id] = True

        await self.bot.send_card(
            chat_id,
            "Task Accepted",
            f"**Prompt:**\n```\n{text}\n```\n\nCodex is working on it...",
        )

        try:
            result = await self.runner.run(text)
            await self.bot.send_card(chat_id, "Task Completed", result)
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
    args = parser.parse_args()

    work_dir = os.path.abspath(args.work_dir)
    if not os.path.isdir(work_dir):
        logger.error("Working directory does not exist: {}", work_dir)
        return

    service = CodexFeishuService(work_dir, args.codex_bin)

    try:
        asyncio.run(service.start())
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()
