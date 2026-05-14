"""Claude Code CLI adapter — spawns a long-lived claude subprocess in stream-json mode
with a custom MCP server (`mcp_server.py`) registered for synchronous user prompts.

Why MCP for user prompts?
- AskUserQuestion in `claude -p --input-format stream-json` does NOT actually
  block on tool_use waiting for a tool_result — claude advances on its own
  with a default response, then any stdin write is treated as a fresh user
  message rather than the answer. Confirmed empirically (see CLAUDE.md).
- MCP tool calls ARE truly synchronous: claude blocks the assistant turn
  until the MCP server returns. We exploit this by having `mcp_server.py`'s
  `ask_user` tool HTTP-POST to backend, where the request waits on an
  asyncio.Future that the user's `/api/answer` POST completes.

Why stream-json mode at all?
- One subprocess covers the whole session: follow-up user messages are written
  to stdin as newline-delimited JSON, no `--resume` churn between turns.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Optional
from uuid import uuid4

from .base import SessionInterface, SessionEvent, EventType, QuestionData

logger = logging.getLogger(__name__)

# Path to our MCP server, sibling of this file's parent (paper-lens-backend/).
_MCP_SERVER_PATH = str(Path(__file__).resolve().parent.parent / "mcp_server.py")


class ClaudeCLIAdapter(SessionInterface):
    """Long-lived claude subprocess driven via stream-json on stdin/stdout."""

    def __init__(self, working_dir: str):
        self.working_dir = working_dir
        self.session_id: str | None = None
        self.claude_session_id: str | None = None  # Claude's internal session ID
        self.process: asyncio.subprocess.Process | None = None
        self._event_queue: asyncio.Queue[SessionEvent] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._has_streaming: bool = False  # Set True when we see stream_event
        self._subscribers: int = 0  # SSE consumers currently attached; nonzero blocks TTL reap
        self._pending_question_id: str | None = None  # tool_use_id of a parked AskUserQuestion

    async def start(self, prompt: str) -> str:
        self.session_id = str(uuid4())
        self._has_streaming = False
        await self._spawn_claude(prompt)
        return self.session_id

    async def send_message(self, message: str) -> None:
        """Send a follow-up user message on stdin (claude is still running)."""
        # If somehow the process died (crash, manual kill, etc.), respawn with --resume.
        if not self._process_alive():
            self._has_streaming = False
            await self._spawn_claude(message, resume=True)
            return
        self._has_streaming = False
        self._pending_question_id = None  # any new user message implicitly cancels the parked question
        await self._send_user_message(message)

    async def answer_question(self, answer_text: str) -> bool:
        """Resolve a parked AskUserQuestion by writing a tool_result on stdin.

        Returns True if there was a parked question and it was resolved; False
        otherwise (caller falls back to send_message).
        """
        if not self._pending_question_id or not self._process_alive():
            return False
        tool_use_id = self._pending_question_id
        self._pending_question_id = None
        await self._send_tool_result(tool_use_id, answer_text)
        return True

    async def events(self) -> AsyncIterator[SessionEvent]:
        # TURN_DONE means "this assistant turn ended"; the session is still alive
        # waiting for the next user message. Only DONE (explicit stop / process
        # exit) or ERROR closes the SSE stream.
        while True:
            event = await self._event_queue.get()
            yield event
            if event.type in (EventType.DONE, EventType.ERROR):
                break

    async def stop(self) -> None:
        # Close stdin so claude finishes pending work and exits cleanly; fall
        # back to terminate/kill if it doesn't shut down within timeout.
        if self.process and self.process.stdin and not self.process.stdin.is_closing():
            try:
                self.process.stdin.close()
            except Exception:
                pass
        await self._cleanup_process()
        await self._event_queue.put(SessionEvent(type=EventType.DONE))

    # ── internals ────────────────────────────────────────────────────────

    def _process_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def _spawn_claude(self, initial_message: str, resume: bool = False) -> None:
        # Inline MCP config: registers our paper_lens server (provides
        # `ask_user`). Session id is passed through env so the server knows
        # which backend session to bind questions to.
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
            "claude",
            "-p",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--mcp-config", json.dumps(mcp_config, ensure_ascii=False),
            # AskUserQuestion is intentionally OMITTED — built-in version
            # does not park reliably in -p stream-json mode. Claude must
            # use mcp__paper_lens__ask_user instead, enforced by prompt.
            "--allowedTools",
            "Read,Write,Edit,Bash,Glob,Grep,Skill,Agent,ToolSearch,mcp__paper_lens__ask_user",
        ]
        if resume and self.claude_session_id:
            cmd.extend(["--resume", self.claude_session_id])

        logger.info(f"Spawning claude (stream-json + MCP paper_lens): session={self.session_id}")
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_dir,
            limit=4 * 1024 * 1024,  # 4MB line buffer
        )
        # Send the initial user message via stream-json. Stay open for follow-ups.
        await self._send_user_message(initial_message)
        self._reader_task = asyncio.create_task(self._read_output())

    async def _send_user_message(self, text: str) -> None:
        """Write a plain user message to claude's stdin."""
        envelope = {
            "type": "user",
            "message": {"role": "user", "content": text},
        }
        await self._write_stdin_json(envelope)

    async def _send_tool_result(self, tool_use_id: str, content: str) -> None:
        """Write a tool_result for a parked tool call to claude's stdin."""
        logger.info(f"[AUQ] tool_result sent: tool_use_id={tool_use_id} content={content[:120]!r}")
        envelope = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                    }
                ],
            },
        }
        await self._write_stdin_json(envelope)

    async def _write_stdin_json(self, obj: dict) -> None:
        if not self.process or not self.process.stdin:
            return
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            self.process.stdin.write(line.encode("utf-8"))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.warning(f"stdin write failed (claude likely exited): {e}")

    async def _read_output(self) -> None:
        """Read stream-json output from claude and parse into events."""
        if not self.process or not self.process.stdout:
            return

        try:
            async for line in self.process.stdout:
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON output: {line[:100]}")
                    continue
                for event in self._parse_event(data):
                    await self._event_queue.put(event)

            # stdout closed → process exited.
            return_code = await self.process.wait()
            if return_code != 0 and self.process.stderr:
                stderr = await self.process.stderr.read()
                error_msg = stderr.decode("utf-8").strip()
                if error_msg:
                    logger.error(f"Claude stderr (rc={return_code}): {error_msg}")
                await self._event_queue.put(
                    SessionEvent(type=EventType.ERROR, data=error_msg or f"claude exited with code {return_code}")
                )
            else:
                # Clean exit (stdin closed by stop() or peer hangup) — session is over.
                await self._event_queue.put(SessionEvent(type=EventType.DONE))

        except Exception as e:
            logger.error(f"Error reading claude output: {e}")
            await self._event_queue.put(
                SessionEvent(type=EventType.ERROR, data=str(e))
            )

    def _parse_event(self, data: dict) -> list[SessionEvent]:
        """Parse a single stream-json event into SessionEvents."""
        events = []
        event_type = data.get("type")

        if event_type == "system" and data.get("subtype") == "init":
            self.claude_session_id = data.get("session_id")
            events.append(SessionEvent(
                type=EventType.STATUS,
                data={"status": "initialized", "session_id": self.claude_session_id}
            ))

        elif event_type == "stream_event":
            self._has_streaming = True
            inner = data.get("event", {})
            events.extend(self._parse_stream_event(inner))

        elif event_type == "assistant":
            # Complete turn summary. If streaming was active, text was already
            # sent via stream_events — only extract structured tool data.
            if not self._has_streaming:
                events.append(SessionEvent(
                    type=EventType.STATUS, data={"status": "new_turn"}
                ))
            message = data.get("message", {})
            content_blocks = message.get("content", [])

            usage = message.get("usage", {})
            if usage:
                events.append(SessionEvent(
                    type=EventType.USAGE,
                    data={
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                    },
                ))

            for block in content_blocks:
                if block.get("type") == "text" and not self._has_streaming:
                    text = block.get("text", "")
                    if text:
                        events.append(SessionEvent(
                            type=EventType.TEXT_DELTA, data=text
                        ))

                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    tool_id = block.get("id", "")

                    if tool_name == "AskUserQuestion":
                        # Legacy path: claude shouldn't be calling this anymore
                        # (it's not in --allowedTools), but if a stale skill
                        # somehow invokes it we still emit a QUESTION event.
                        self._pending_question_id = tool_id
                        questions = tool_input.get("questions", [])
                        logger.warning(
                            f"[AUQ-LEGACY] AskUserQuestion called despite being disallowed. "
                            f"tool_use_id={tool_id} questions={len(questions)}"
                        )
                        events.append(SessionEvent(
                            type=EventType.QUESTION,
                            data=QuestionData(questions=questions)
                        ))
                        continue

                    if tool_name == "mcp__paper_lens__ask_user":
                        # The MCP server pushes QUESTION events to the SSE
                        # stream itself (via /api/mcp/ask-user → backend). The
                        # tool_use card here is just visual feedback that the
                        # tool was called; suppress the duplicate question render.
                        questions = tool_input.get("questions", [])
                        logger.info(
                            f"[MCP-AUQ] mcp__paper_lens__ask_user invoked: "
                            f"tool_use_id={tool_id} questions={len(questions)}"
                        )
                        # Skip the generic TOOL_USE emit too — the user sees
                        # the question popup, not a "tool used" card.
                        continue

                    if tool_name in ("Write", "Edit"):
                        file_path = tool_input.get("file_path", "")
                        if file_path:
                            events.append(SessionEvent(
                                type=EventType.FILE_SAVED,
                                data={"path": file_path, "tool": tool_name}
                            ))

                    events.append(SessionEvent(
                        type=EventType.TOOL_USE,
                        data={"tool": tool_name, "input": tool_input, "id": tool_id},
                    ))

        elif event_type == "user":
            # User messages echoed back by the CLI — typically tool_result blocks
            # (either ours via _send_tool_result, or the CLI's own builtin tool execution).
            message = data.get("message", {})
            content_blocks = message.get("content", [])
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id", "")
                        is_error = bool(block.get("is_error", False))
                        raw_content = block.get("content", "")
                        if isinstance(raw_content, list):
                            parts = []
                            for p in raw_content:
                                if isinstance(p, dict):
                                    parts.append(p.get("text", ""))
                                else:
                                    parts.append(str(p))
                            content_text = "\n".join(parts)
                        else:
                            content_text = str(raw_content)
                        events.append(SessionEvent(
                            type=EventType.TOOL_RESULT,
                            data={
                                "id": tool_id,
                                "content": content_text,
                                "is_error": is_error,
                            },
                        ))

        elif event_type == "result":
            # End of one assistant turn. Session stays alive on stdin waiting for
            # the next user message. Reset streaming flag so the next turn's
            # text comes in fresh.
            logger.info(
                f"[AUQ] result/TURN_DONE: pending_question={self._pending_question_id} "
                f"is_error={data.get('is_error')} subtype={data.get('subtype')}"
            )
            self._has_streaming = False
            events.append(SessionEvent(type=EventType.TURN_DONE))

        return events

    def _parse_stream_event(self, inner: dict) -> list[SessionEvent]:
        """Parse an inner stream_event (Anthropic Messages API format)."""
        events = []
        inner_type = inner.get("type")

        if inner_type == "message_start":
            events.append(SessionEvent(
                type=EventType.STATUS, data={"status": "new_turn"}
            ))

        elif inner_type == "content_block_start":
            block = inner.get("content_block", {})
            if block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                tool_id = block.get("id", "")
                # Suppress the streaming tool_use card for the MCP question
                # tool — the user already sees the question popup.
                if tool_name == "mcp__paper_lens__ask_user":
                    return events
                if tool_name:
                    events.append(SessionEvent(
                        type=EventType.TOOL_USE,
                        data={"tool": tool_name, "id": tool_id}
                    ))

        elif inner_type == "content_block_delta":
            delta = inner.get("delta", {})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text", "")
                if text:
                    events.append(SessionEvent(
                        type=EventType.TEXT_DELTA, data=text
                    ))
            elif delta_type == "thinking_delta":
                text = delta.get("thinking", "")
                if text:
                    events.append(SessionEvent(
                        type=EventType.THINKING_DELTA, data=text
                    ))

        return events

    async def _cleanup_process(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self.process:
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
