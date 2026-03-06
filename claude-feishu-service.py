"""
Feishu + Claude Code integration service.

Receives development requirements from Feishu messages,
invokes Claude Code CLI to execute them,
and sends execution logs back to Feishu.

Usage:
    pip install lark-oapi loguru
    python claude-feishu-service.py [--work-dir /path/to/project]
"""

import argparse
import asyncio
import json
import os
import subprocess
import threading
import time
from collections import OrderedDict
from typing import Any

from loguru import logger

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

# ── Config ────────────────────────────────────────────────────────────────────
from config import APP_SECRET, APP_ID

# Maximum characters per Feishu card message (leave margin for JSON overhead)
MAX_CARD_CONTENT_LEN = 28000


# ── Feishu helpers ────────────────────────────────────────────────────────────

class FeishuBot:
    """Minimal Feishu bot: receive messages via WebSocket, send replies."""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._client: lark.Client | None = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._processed_ids: OrderedDict[str, None] = OrderedDict()
        self._message_handler = None  # async callback(sender_id, chat_id, chat_type, text)

    def on_message(self, handler):
        """Register an async message handler."""
        self._message_handler = handler

    async def start(self):
        self._running = True
        self._loop = asyncio.get_running_loop()

        self._client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .build()
        )

        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        def run_ws():
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.warning("WebSocket error: {}", e)
                if self._running:
                    time.sleep(5)

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        logger.info("Feishu bot started (WebSocket)")

        while self._running:
            await asyncio.sleep(1)

    async def stop(self):
        self._running = False
        logger.info("Feishu bot stopped")

    # ── receive ───────────────────────────────────────────────────────────

    def _on_message_sync(self, data) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data) -> None:
        try:
            event = data.event
            message = event.message
            sender = event.sender

            msg_id = message.message_id
            if msg_id in self._processed_ids:
                return
            self._processed_ids[msg_id] = None
            while len(self._processed_ids) > 1000:
                self._processed_ids.popitem(last=False)

            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            # Only handle text and post messages
            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            text = ""
            if msg_type == "text":
                text = content_json.get("text", "").strip()
            elif msg_type == "post":
                text = self._extract_post_text(content_json).strip()
            else:
                logger.info("Ignoring non-text message type: {}", msg_type)
                return

            if not text:
                return

            # Remove @bot mentions (format: @_user_1 or similar)
            import re
            text = re.sub(r"@_user_\d+\s*", "", text).strip()
            if not text:
                return

            reply_to = chat_id if chat_type == "group" else sender_id

            logger.info("Received message from {}: {}", sender_id, text)

            if self._message_handler:
                await self._message_handler(sender_id, reply_to, chat_type, text)

        except Exception as e:
            logger.error("Error processing message: {}", e)

    @staticmethod
    def _extract_post_text(content_json: dict) -> str:
        """Extract plain text from post (rich text) message."""
        root = content_json
        if isinstance(root.get("post"), dict):
            root = root["post"]
        if not isinstance(root, dict):
            return ""

        def parse_block(block):
            if not isinstance(block, dict) or not isinstance(block.get("content"), list):
                return ""
            parts = []
            if title := block.get("title"):
                parts.append(title)
            for row in block["content"]:
                if not isinstance(row, list):
                    continue
                for el in row:
                    if not isinstance(el, dict):
                        continue
                    tag = el.get("tag")
                    if tag in ("text", "a"):
                        parts.append(el.get("text", ""))
                    elif tag == "at":
                        parts.append(f"@{el.get('user_name', 'user')}")
            return " ".join(parts).strip()

        if "content" in root:
            t = parse_block(root)
            if t:
                return t

        for key in ("zh_cn", "en_us", "ja_jp"):
            if key in root and isinstance(root[key], dict):
                t = parse_block(root[key])
                if t:
                    return t
        return ""

    # ── send ──────────────────────────────────────────────────────────────

    def _send_message_sync(self, receive_id_type: str, receive_id: str, msg_type: str, content: str) -> bool:
        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error("Send failed: code={}, msg={}", response.code, response.msg)
                return False
            return True
        except Exception as e:
            logger.error("Error sending message: {}", e)
            return False

    async def send_text(self, chat_id: str, text: str):
        """Send a plain text message."""
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
        content = json.dumps({"text": text}, ensure_ascii=False)
        logger.info("Sending text to {}: {}", chat_id, text)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._send_message_sync, receive_id_type, chat_id, "text", content
        )

    async def send_card(self, chat_id: str, title: str, content: str):
        """Send a Feishu interactive card message, auto-splitting if too long."""
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
        loop = asyncio.get_running_loop()

        chunks = self._split_content(content, MAX_CARD_CONTENT_LEN)
        total = len(chunks)

        for i, chunk in enumerate(chunks):
            card_title = title if total == 1 else f"{title} ({i + 1}/{total})"
            logger.info("Sending card to {} [{}]: {}", chat_id, card_title, chunk)
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": card_title},
                    "template": "green" if "completed" in title.lower() or "done" in title.lower() else "blue",
                },
                "elements": [
                    {"tag": "markdown", "content": chunk},
                ],
            }
            card_json = json.dumps(card, ensure_ascii=False)
            await loop.run_in_executor(
                None, self._send_message_sync, receive_id_type, chat_id, "interactive", card_json
            )

    @staticmethod
    def _split_content(text: str, max_len: int) -> list[str]:
        """Split long text into chunks, preferring line boundaries."""
        if len(text) <= max_len:
            return [text]
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Find a good split point (newline near the limit)
            split_at = text.rfind("\n", 0, max_len)
            if split_at < max_len // 2:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks


# ── Claude Code runner ────────────────────────────────────────────────────────

class ClaudeCodeRunner:
    """Run Claude Code CLI and capture output."""

    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        self._tasks: dict[str, asyncio.Task] = {}

    async def run(self, prompt: str, task_id: str | None = None) -> str:
        """
        Invoke `claude` CLI with the given prompt and return the output.

        Uses `claude --print` mode for non-interactive execution.
        """
        claude_cmd = "/home/liuyong/.nvm/versions/node/v25.7.0/bin/claude"
        cmd = [
            claude_cmd,
            "--dangerously-skip-permissions",
            "--print",           # non-interactive, print result and exit
            "--output-format", "text",
            prompt,
        ]

        logger.info("Running Claude Code: {}", prompt)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.work_dir,
            )

            stdout, stderr = await proc.communicate()
            output = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                result = f"**Exit code:** {proc.returncode}\n\n"
                if output:
                    result += f"**Output:**\n```\n{output}\n```\n\n"
                if err:
                    result += f"**Stderr:**\n```\n{err}\n```"
                return result.strip()

            return output if output else "(no output)"

        except FileNotFoundError as e:
            return f"Error: `{claude_cmd}` command not found. Make sure Claude Code CLI is installed and in PATH. Details: {str(e)}"
        except Exception as e:
            return f"Error running Claude Code: {e}"


# ── Main service ──────────────────────────────────────────────────────────────

class ClaudeFeishuService:
    """Bridges Feishu messages to Claude Code execution."""

    # Commands that are handled specially instead of forwarded to Claude Code
    HELP_TEXT = """**Claude Code Feishu Bot**

Send me a development task and I'll use Claude Code to work on it.

**Commands:**
- Send any text -> Execute as Claude Code prompt
- `/status` -> Check if the service is running
- `/help` -> Show this help message

**Tips:**
- Be specific about what you want (e.g., "Add a login page to src/app.py")
- The working directory is: `{work_dir}`
"""

    def __init__(self, work_dir: str):
        self.bot = FeishuBot(APP_ID, APP_SECRET)
        self.runner = ClaudeCodeRunner(work_dir)
        self.work_dir = work_dir
        self._active_tasks: dict[str, bool] = {}  # chat_id -> is_running

        self.bot.on_message(self._handle_message)

    async def start(self):
        logger.info("Claude Code Feishu Service starting...")
        logger.info("Working directory: {}", self.work_dir)
        await self.bot.start()

    async def _handle_message(self, sender_id: str, chat_id: str, chat_type: str, text: str):
        """Handle an incoming Feishu message."""

        # Built-in commands
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
                f"**Service status:** {status}\n**Working directory:** `{self.work_dir}`",
            )
            return

        # Check if there's already a task running for this chat
        if self._active_tasks.get(chat_id):
            await self.bot.send_text(
                chat_id,
                "A task is already running for this chat. Please wait for it to finish.",
            )
            return

        # Execute Claude Code task
        self._active_tasks[chat_id] = True

        await self.bot.send_card(
            chat_id,
            "Task Accepted",
            f"**Prompt:**\n```\n{text}\n```\n\nClaude Code is working on it...",
        )

        try:
            result = await self.runner.run(text)

            # Format the result nicely
            await self.bot.send_card(
                chat_id,
                "Task Completed",
                result,
            )

        except Exception as e:
            logger.error("Task error: {}", e)
            await self.bot.send_card(
                chat_id,
                "Task Failed",
                f"**Error:**\n```\n{e}\n```",
            )
        finally:
            self._active_tasks[chat_id] = False


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu + Claude Code integration service")
    parser.add_argument(
        "--work-dir",
        default=os.getcwd(),
        help="Working directory for Claude Code (default: current directory)",
    )
    args = parser.parse_args()

    work_dir = os.path.abspath(args.work_dir)
    if not os.path.isdir(work_dir):
        logger.error("Working directory does not exist: {}", work_dir)
        return

    service = ClaudeFeishuService(work_dir)

    try:
        asyncio.run(service.start())
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()
