#!/usr/bin/env python3
"""
Paper-Lens MCP server (stdio).

Provides a single tool, `ask_user`, that synchronously blocks until the user
answers via the paper-lens Web UI. Used in place of the built-in
AskUserQuestion tool, which does NOT actually park on tool_use in
`claude -p --input-format stream-json` mode.

Why MCP works where AskUserQuestion fails:
  Claude treats MCP tool calls as truly synchronous — the assistant turn
  blocks until the tool returns. We exploit this by having the tool's
  HTTP request to backend block (server-side asyncio.Future) until the
  user submits an answer through the UI.

Wire format:
  claude  ──stdio JSON-RPC──>  this server
  this server  ──HTTP POST /api/mcp/ask-user──>  paper-lens-backend
  backend  (blocks on Future, emits QUESTION event to UI)
  user submits  /api/answer  →  Future completes
  HTTP POST returns with answer text
  this server returns tool_result to claude
"""
import json
import os
import sys
import urllib.request
import urllib.error

BACKEND = os.environ.get("PAPER_LENS_BACKEND", "http://localhost:8765")
SESSION_ID = os.environ.get("PAPER_LENS_SESSION_ID", "")
ASK_TIMEOUT_SECONDS = int(os.environ.get("PAPER_LENS_ASK_TIMEOUT", "1800"))

PROTOCOL_VERSION = "2024-11-05"

TOOL_DEFINITION = {
    "name": "ask_user",
    "description": (
        "Ask the user one or more structured questions and BLOCK until they "
        "answer in the paper-lens Web UI. Use this every time the skill "
        "instructions say to use AskUserQuestion — they have identical "
        "input schemas. The tool returns the user's answer as plain text. "
        "Always prefer this over plain-text questions: this gives the user "
        "an interactive popup with checkboxes."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "Questions to render in one popup (1-3 recommended).",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "header": {"type": "string"},
                        "multiSelect": {"type": "boolean"},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label"],
                            },
                        },
                    },
                    "required": ["question", "options"],
                },
            }
        },
        "required": ["questions"],
    },
}


def write_message(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def call_backend_ask(questions: list) -> str:
    """POST to backend; this call blocks until the user answers (or timeout)."""
    if not SESSION_ID:
        return "[paper-lens MCP error: PAPER_LENS_SESSION_ID env var not set]"
    payload = json.dumps(
        {"session_id": SESSION_ID, "questions": questions}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{BACKEND}/api/mcp/ask-user",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=ASK_TIMEOUT_SECONDS + 5) as r:
            data = json.loads(r.read())
            return data.get("answer", "")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:
            body = ""
        return f"[paper-lens backend error: HTTP {e.code} {body}]"
    except Exception as e:
        return f"[paper-lens backend unreachable: {e}]"


def handle(msg: dict):
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "paper-lens", "version": "1.0.0"},
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notifications have no response

    if method == "tools/list":
        return _ok(req_id, {"tools": [TOOL_DEFINITION]})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name != "ask_user":
            return _err(req_id, -32601, f"Unknown tool: {name}")
        questions = args.get("questions") or []
        if not isinstance(questions, list) or not questions:
            return _err(req_id, -32602, "questions must be a non-empty list")
        answer = call_backend_ask(questions)
        return _ok(req_id, {
            "content": [{"type": "text", "text": answer}],
            "isError": False,
        })

    if req_id is not None:
        return _err(req_id, -32601, f"Unknown method: {method}")
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            response = handle(msg)
        except Exception as e:
            response = _err(msg.get("id"), -32000, f"internal error: {e}")
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    main()
