"""Tests for adapter event parsing."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.base import EventType, QuestionData
from adapters.claude_cli import ClaudeCLIAdapter


class TestEventParsing:
    """Test ClaudeCLIAdapter._parse_event with real stream-json formats."""

    def setup_method(self):
        self.adapter = ClaudeCLIAdapter(working_dir="/tmp")

    def test_parse_init_event(self):
        data = {
            "type": "system",
            "subtype": "init",
            "session_id": "abc-123",
            "model": "claude-opus-4-6",
        }
        events = self.adapter._parse_event(data)
        assert len(events) == 1
        assert events[0].type == EventType.STATUS
        assert self.adapter.claude_session_id == "abc-123"

    # --- Fallback mode (no stream_events, _has_streaming=False) ---

    def test_fallback_text_content(self):
        """Without streaming, assistant events emit new_turn + text."""
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "这篇论文提出了一个新框架"}
                ]
            }
        }
        events = self.adapter._parse_event(data)
        assert len(events) == 2
        assert events[0].type == EventType.STATUS
        assert events[0].data["status"] == "new_turn"
        assert events[1].type == EventType.TEXT_DELTA
        assert events[1].data == "这篇论文提出了一个新框架"

    def test_fallback_multiple_blocks(self):
        """Without streaming, all content blocks are emitted."""
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "正在分析..."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    {"type": "text", "text": "分析结果如下"},
                ]
            }
        }
        events = self.adapter._parse_event(data)
        assert len(events) == 4  # new_turn + 3 blocks
        assert events[0].data["status"] == "new_turn"
        assert events[1].type == EventType.TEXT_DELTA
        assert events[2].type == EventType.TOOL_USE
        assert events[3].type == EventType.TEXT_DELTA

    # --- Streaming mode (stream_events present, _has_streaming=True) ---

    def test_stream_event_text_delta(self):
        """stream_event with text_delta emits TEXT_DELTA."""
        events = self.adapter._parse_event({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"}
            }
        })
        assert len(events) == 1
        assert events[0].type == EventType.TEXT_DELTA
        assert events[0].data == "Hello"
        assert self.adapter._has_streaming is True

    def test_stream_event_message_start(self):
        """message_start emits new_turn to clear tool indicators."""
        events = self.adapter._parse_event({
            "type": "stream_event",
            "event": {"type": "message_start", "message": {}}
        })
        assert len(events) == 1
        assert events[0].type == EventType.STATUS
        assert events[0].data["status"] == "new_turn"

    def test_stream_event_tool_start(self):
        """content_block_start with tool_use emits TOOL_USE indicator."""
        events = self.adapter._parse_event({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "Read"}
            }
        })
        assert len(events) == 1
        assert events[0].type == EventType.TOOL_USE
        assert events[0].data["tool"] == "Read"

    def test_streaming_assistant_skips_text(self):
        """After stream_events, assistant event skips text blocks."""
        # Simulate streaming active
        self.adapter._has_streaming = True
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "already streamed"},
                    {"type": "tool_use", "name": "Write", "input": {
                        "file_path": "/path/to/file.md", "content": "# Notes"
                    }},
                ]
            }
        }
        events = self.adapter._parse_event(data)
        # Should have FILE_SAVED plus a TOOL_USE update carrying the final input
        # (no new_turn, no TEXT_DELTA).
        assert len(events) == 2
        assert events[0].type == EventType.FILE_SAVED
        assert events[0].data["path"] == "/path/to/file.md"
        assert events[1].type == EventType.TOOL_USE
        assert events[1].data["tool"] == "Write"

    def test_streaming_assistant_ask_user_question(self):
        """AskUserQuestion is still extracted from assistant event in streaming mode."""
        self.adapter._has_streaming = True
        data = {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [{
                            "question": "选择术语",
                            "options": [{"label": "全部"}],
                            "multiSelect": True
                        }]
                    }
                }]
            }
        }
        events = self.adapter._parse_event(data)
        assert len(events) == 1
        assert events[0].type == EventType.QUESTION
        assert isinstance(events[0].data, QuestionData)

    # --- Structured data (always processed) ---

    def test_parse_file_write(self):
        data = {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Write",
                    "input": {
                        "file_path": "/path/to/paper-notes/test/deep-learn.md",
                        "content": "# Notes"
                    }
                }]
            }
        }
        events = self.adapter._parse_event(data)
        # Without streaming: new_turn + file_saved
        assert any(e.type == EventType.FILE_SAVED for e in events)
        file_event = next(e for e in events if e.type == EventType.FILE_SAVED)
        assert file_event.data["path"] == "/path/to/paper-notes/test/deep-learn.md"

    def test_parse_result_event(self):
        data = {
            "type": "result",
            "subtype": "success",
            "result": "分析完成",
            "duration_ms": 5000,
        }
        events = self.adapter._parse_event(data)
        assert len(events) == 1
        assert events[0].type == EventType.TURN_DONE

    def test_parse_unknown_event_returns_empty(self):
        data = {"type": "rate_limit_event", "rate_limit_info": {}}
        events = self.adapter._parse_event(data)
        assert len(events) == 0

    # --- End-to-end streaming sequence ---

    def test_full_streaming_sequence(self):
        """Simulate a complete streaming turn: message_start → text → tool → assistant."""
        adapter = ClaudeCLIAdapter(working_dir="/tmp")

        # 1. message_start
        e1 = adapter._parse_event({
            "type": "stream_event",
            "event": {"type": "message_start", "message": {}}
        })
        assert e1[0].data["status"] == "new_turn"

        # 2. text delta
        e2 = adapter._parse_event({
            "type": "stream_event",
            "event": {"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": "Let me read"}}
        })
        assert e2[0].type == EventType.TEXT_DELTA

        # 3. tool start
        e3 = adapter._parse_event({
            "type": "stream_event",
            "event": {"type": "content_block_start",
                      "content_block": {"type": "tool_use", "name": "Read", "id": "tool-1"}}
        })
        assert e3[0].type == EventType.TOOL_USE

        # 4. assistant summary (text already streamed, only extract tool data)
        e4 = adapter._parse_event({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "Let me read"},
                {"type": "tool_use", "name": "Read", "id": "tool-1", "input": {"file_path": "/tmp/x"}},
            ]}
        })
        # Text should be skipped (already streamed); tool summary updates the card with input.
        assert len(e4) == 1
        assert e4[0].type == EventType.TOOL_USE
        assert e4[0].data["id"] == "tool-1"

        # 5. result
        e5 = adapter._parse_event({
            "type": "result", "result": "Done"
        })
        assert e5[0].type == EventType.TURN_DONE
