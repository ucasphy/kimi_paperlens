"""Kimi Code CLI adapter — spawns kimi subprocess in --print + stream-json mode.

Unlike Claude CLI's long-lived stream-json process, Kimi CLI's --print mode
exits after each turn. We use --resume to continue sessions across turns.

Output format (line-delimited JSON):
  {"role":"assistant","content":[{"type":"think",...},{"type":"text",...}],"tool_calls":[...]}
  {"role":"tool","content":"...","tool_call_id":"..."}
  {"role":"assistant","content":[...]}

Stderr contains:
  To resume this session: kimi -r <session_id>
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from .base import SessionInterface, SessionEvent, EventType, QuestionData

logger = logging.getLogger(__name__)

# Path to our MCP server, sibling of this file's parent (paper-lens-backend/).
_MCP_SERVER_PATH = str(Path(__file__).resolve().parent.parent / "mcp_server.py")

# Kimi CLI binary path (discovered at runtime)
_KIMI_CLI_PATH = os.environ.get("KIMI_CLI_PATH", "kimi")


class KimiCLIAdapter(SessionInterface):
    """Kimi CLI subprocess driven via --print --output-format stream-json."""

    def __init__(self, working_dir: str):
        self.working_dir = working_dir
        self.session_id: str | None = None
        self.kimi_session_id: str | None = None
        self._event_queue: asyncio.Queue[SessionEvent] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._subscribers: int = 0

    async def start(self, prompt: str) -> str:
        self.session_id = str(uuid4())
        await self._spawn_kimi(prompt)
        return self.session_id

    async def send_message(self, message: str) -> None:
        """Send a follow-up user message by spawning a resumed kimi process."""
        await self._spawn_kimi(message, resume=True)

    async def events(self) -> AsyncIterator[SessionEvent]:
        while True:
            event = await self._event_queue.get()
            yield event
            if event.type in (EventType.DONE, EventType.ERROR):
                break

    async def stop(self) -> None:
        await self._cleanup_process()
        await self._event_queue.put(SessionEvent(type=EventType.DONE))

    # ── internals ────────────────────────────────────────────────────────

    async def _spawn_kimi(self, message: str, resume: bool = False) -> None:
        backend_port = int(os.environ.get("PORT", 8765))
        mcp_config = {
            "mcpServers": {
                "paper_lens": {
                    "command": "python3",
                    "args": [_MCP_SERVER_PATH],
                    "env": {
                        "PAPER_LENS_SESSION_ID": self.session_id or "",
                        "PAPER_LENS_BACKEND": f"http://localhost:{backend_port}",
                    },
                }
            }
        }

        cmd = [
            _KIMI_CLI_PATH,
            "--print",
            "--output-format", "stream-json",
            "--mcp-config", json.dumps(mcp_config, ensure_ascii=False),
            # Note: kimi --print auto-approves tools; we rely on MCP for AskUserQuestion.
        ]
        if resume and self.kimi_session_id:
            cmd.extend(["--resume", self.kimi_session_id])

        logger.info(f"Spawning kimi (stream-json + MCP paper_lens): session={self.session_id} resume={resume}")

        # Cancel any previous reader before starting a new turn
        await self._cleanup_process()

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_dir,
        )

        # Send message via stdin (text mode for kimi --print)
        stdin_line = (message + "\n").encode("utf-8")
        try:
            self.process.stdin.write(stdin_line)
            await self.process.stdin.drain()
            self.process.stdin.close()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.warning(f"stdin write failed (kimi likely exited): {e}")

        self._reader_task = asyncio.create_task(self._read_output())

    async def _read_output(self) -> None:
        """Read stream-json output from kimi and parse into events."""
        if not self.process or not self.process.stdout:
            return

        try:
            stdout_lines = []
            async for line in self.process.stdout:
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                stdout_lines.append(line)
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON stdout: {line[:100]}")
                    continue
                for event in self._parse_event(data):
                    await self._event_queue.put(event)

            # Read stderr to extract session ID
            stderr_data = await self.process.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            if stderr_text:
                # Extract session ID from "To resume this session: kimi -r <uuid>"
                match = re.search(r'To resume this session: kimi -r ([0-9a-f-]+)', stderr_text)
                if match:
                    self.kimi_session_id = match.group(1)
                    logger.info(f"Kimi session id: {self.kimi_session_id}")
                else:
                    logger.debug(f"Kimi stderr: {stderr_text[:200]}")

            return_code = await self.process.wait()
            if return_code != 0:
                logger.warning(f"Kimi exited with code {return_code}")

            # Turn is complete
            await self._event_queue.put(SessionEvent(type=EventType.TURN_DONE))

        except Exception as e:
            logger.error(f"Error reading kimi output: {e}")
            await self._event_queue.put(
                SessionEvent(type=EventType.ERROR, data=str(e))
            )

    def _parse_event(self, data: dict) -> list[SessionEvent]:
        """Parse a single kimi stream-json line into SessionEvents."""
        events = []
        role = data.get("role")

        if role == "assistant":
            content_blocks = data.get("content", [])
            # Defensive: content may be a plain string in edge cases
            if isinstance(content_blocks, str):
                if content_blocks:
                    events.append(SessionEvent(
                        type=EventType.TEXT_DELTA, data=content_blocks
                    ))
            elif isinstance(content_blocks, list):
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "think":
                        think_text = block.get("think", "")
                        if think_text:
                            events.append(SessionEvent(
                                type=EventType.THINKING_DELTA, data=think_text
                            ))
                    elif block_type == "text":
                        text = block.get("text", "")
                        if text:
                            events.append(SessionEvent(
                                type=EventType.TEXT_DELTA, data=text
                            ))

            # Tool calls
            tool_calls = data.get("tool_calls", [])
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function", {}) or {}
                tool_name = func.get("name", "") if isinstance(func, dict) else ""
                tool_input = {}
                try:
                    args = func.get("arguments", "{}") if isinstance(func, dict) else "{}"
                    tool_input = json.loads(args)
                except (json.JSONDecodeError, AttributeError):
                    pass
                tool_id = tc.get("id", "")

                # Map MCP ask_user to QUESTION event.
                # NOTE: For Kimi CLI, the QUESTION event is already pushed to
                # the queue by server.py's mcp_ask_user endpoint (which blocks
                # on the Future). We skip the duplicate here to avoid double
                # question popups in the UI.
                if tool_name == "ask_user" or tool_name == "mcp__paper_lens__ask_user":
                    questions = tool_input.get("questions", []) if isinstance(tool_input, dict) else []
                    logger.info(f"[MCP-AUQ] ask_user invoked: questions={len(questions)} (suppressed duplicate QUESTION event)")
                    # Skip TOOL_USE emit too — the user sees the question popup.
                    continue

                if tool_name in ("Write", "Edit"):
                    file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
                    if file_path:
                        events.append(SessionEvent(
                            type=EventType.FILE_SAVED,
                            data={"path": file_path, "tool": tool_name}
                        ))

                events.append(SessionEvent(
                    type=EventType.TOOL_USE,
                    data={"tool": tool_name, "input": tool_input, "id": tool_id},
                ))

        elif role == "tool":
            tool_id = data.get("tool_call_id", "")
            content = data.get("content", "")
            # Normalize list-of-dicts content to plain text
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        parts.append(p.get("text", ""))
                    else:
                        parts.append(str(p))
                content = "\n".join(parts)
            elif not isinstance(content, str):
                content = str(content)
            events.append(SessionEvent(
                type=EventType.TOOL_RESULT,
                data={"id": tool_id, "content": content, "is_error": False},
            ))

        return events

    async def _cleanup_process(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self._reader_task = None

        if getattr(self, "process", None):
            try:
                if self.process.returncode is None:
                    self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            self.process = None
