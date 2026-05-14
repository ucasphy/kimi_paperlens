"""Paper-Lens Backend — FastAPI API server (serves paper-lens-web Next.js frontend)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from uuid import uuid4

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Body
from fastapi.responses import FileResponse, StreamingResponse
import uvicorn

from adapters import KimiCLIAdapter, SessionEvent
from adapters.base import EventType, QuestionData

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("paper-lens-backend")

# Paths
BASE_DIR = Path(__file__).parent
PROJECT_DIR = BASE_DIR.parent  # The main project directory
PAPER_NOTES_DIR = PROJECT_DIR / "paper-notes"

# Server port — resolved once at startup, used by adapters
SERVER_PORT = int(os.environ.get("PORT", 8765))

# Path to project uv venv Python (has pymupdf, markdown, matplotlib installed)
VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python3"
if not VENV_PYTHON.exists():
    # Fallback to system python3 if venv doesn't exist
    VENV_PYTHON = Path("python3")

# Global venv python path string (used in prompts / subprocess)
VENV_PYTHON_STR = str(VENV_PYTHON)

# Active sessions: session_id -> (adapter, last_active_timestamp)
sessions: dict[str, tuple[KimiCLIAdapter, float]] = {}
SESSION_TTL_SECONDS = 1800  # 30 minutes — covers long deep-learn turns + AskUserQuestion think time

# Pending MCP-driven user questions: session_id -> {"future": Future, "questions": list}.
# `mcp_server.py`'s `ask_user` tool POSTs to /api/mcp/ask-user, which awaits
# the future. /api/answer completes it with the user's answer text.
mcp_pending: dict[str, dict] = {}


# ── Session cleanup ───────────────────────────────────────────────────

async def _cleanup_expired_sessions() -> None:
    """Periodically remove sessions older than SESSION_TTL_SECONDS.

    Sessions with a live claude subprocess are skipped: while Claude is
    actively producing output, the subprocess is running but no HTTP
    endpoint is hit, so its last_active timestamp would otherwise age
    past TTL and the session would be reaped mid-turn.
    """
    def _is_session_in_use(adapter) -> bool:
        # Live claude subprocess (mid-turn).
        proc = getattr(adapter, "process", None)
        if proc is not None and proc.returncode is None:
            return True
        # SSE stream(s) still attached — browser tab is open and listening.
        if getattr(adapter, "_subscribers", 0) > 0:
            return True
        return False

    while True:
        await asyncio.sleep(60)  # check every minute
        now = time.time()
        expired = [
            sid for sid, (adapter, ts) in sessions.items()
            if now - ts > SESSION_TTL_SECONDS
            and not _is_session_in_use(adapter)
        ]
        for sid in expired:
            adapter, _ = sessions.pop(sid)
            logger.info(f"Cleaning up expired session: {sid}")
            try:
                await adapter.stop()
            except Exception as e:
                logger.warning(f"Error stopping expired session {sid}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    task = asyncio.create_task(_cleanup_expired_sessions())
    yield
    # Shutdown
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Paper-Lens Backend", lifespan=lifespan)

# CORS for Next.js frontend (paper-lens-web)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.get("/")
async def root():
    return {"service": "paper-lens-backend", "frontend": "http://localhost:3000"}


# ── Paper management endpoints ────────────────────────────────────────

@app.get("/api/papers")
async def list_papers():
    """List existing paper-notes directories."""
    if not PAPER_NOTES_DIR.exists():
        return {"papers": []}

    papers = []
    for d in PAPER_NOTES_DIR.iterdir():
        if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("__"):
            files = [f.name for f in d.iterdir() if f.is_file()]
            html_files = [f for f in files if f.endswith('.html')]
            # Viewable note files: analysis outputs (.md), excluding raw/utility files
            _exclude_md = {'extracted-text.md', 'README.md', 'readme.md', 'download-summary.md'}
            note_files = sorted(
                [f for f in files if f.endswith('.md') and f not in _exclude_md],
                key=lambda f: (
                    0 if f.startswith('speed-read') else
                    1 if f.startswith('paper-reading') else
                    2 if f.startswith('deep-learn') else
                    3 if f.startswith('slides-content') else 4,
                    f,
                ),
            )
            # Get most recent modification time from any file in the directory
            mtime = max(
                (f.stat().st_mtime for f in d.iterdir() if f.is_file()),
                default=d.stat().st_mtime,
            )
            papers.append({
                "name": d.name,
                "files": files,
                "note_files": note_files,
                "html_files": html_files,
                "has_speed_read": any(f.startswith('speed-read') and f.endswith('.md') for f in files),
                "has_paper_reading": any(f.startswith('paper-reading') and f.endswith('.md') for f in files),
                "has_deep_learn": any(f.startswith('deep-learn') and f.endswith('.md') for f in files),
                "has_slides": any(f.startswith('slides-content') and f.endswith('.md') for f in files),
                "has_presentation": len(html_files) > 0,
                "presentation_file": html_files[0] if html_files else None,
                "has_pdf": any(f.endswith('.pdf') for f in files),
                "pdf_file": next((f for f in files if f.endswith('.pdf')), None),
                "mtime": mtime,
            })
    # Sort by modification time, most recent first
    papers.sort(key=lambda p: p["mtime"], reverse=True)
    return {"papers": papers}


@app.get("/api/paper/{paper_name}")
async def get_paper_detail(paper_name: str):
    """Get detailed file info for a paper (with metadata for preview loading)."""
    paper_dir = (PAPER_NOTES_DIR / paper_name).resolve()
    if not str(paper_dir).startswith(str(PAPER_NOTES_DIR.resolve())):
        raise HTTPException(403, "Access denied")
    if not paper_dir.exists() or not paper_dir.is_dir():
        raise HTTPException(404, "Paper not found")

    files = []
    for f in sorted(paper_dir.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            stat = f.stat()
            files.append({
                "name": f.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "is_markdown": f.suffix == ".md",
                "is_html": f.suffix == ".html",
                "is_pdf": f.suffix == ".pdf",
            })

    # Also list subdirectories (images/, figures/)
    subdirs = [d.name for d in paper_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]

    return {
        "name": paper_name,
        "files": sorted(files, key=lambda x: -x["mtime"]),
        "subdirs": subdirs,
    }


def _extract_pdf_text_sync(pdf_path: Path, output_path: Path) -> bool:
    """Extract text from PDF using backend's own Python process (which already has fitz)."""
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        text = ""
        for i, page in enumerate(doc):
            text += f"\n\n===== PAGE {i+1} =====\n\n"
            text += page.get_text()
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(text)
        doc.close()
        logger.info(f"Extracted text from {pdf_path} -> {output_path}")
        return True
    except Exception as e:
        logger.warning(f"PDF text extraction failed: {e}")
        return False


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), name: str = Form("")):
    """Upload a PDF and save to paper-notes directory."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file")

    # Derive paper name from filename if not provided
    paper_name = name.strip() or file.filename.rsplit(".", 1)[0]
    paper_name = paper_name.lower().replace(" ", "-").replace("_", "-")

    paper_dir = PAPER_NOTES_DIR / paper_name
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "images").mkdir(exist_ok=True)

    pdf_path = paper_dir / "paper.pdf"
    content = await file.read()
    pdf_path.write_bytes(content)

    # Auto-extract text in background
    extracted_path = paper_dir / "extracted-text.md"
    if _extract_pdf_text_sync(pdf_path, extracted_path):
        logger.info(f"Extracted text for {paper_name}")
    else:
        logger.warning(f"Failed to extract text for {paper_name}")

    return {"paper_name": paper_name, "pdf_path": str(pdf_path)},


@app.post("/api/rename-paper")
async def rename_paper(old_name: str = Form(...), new_name: str = Form(...)):
    """Rename a paper directory."""
    import re

    # Sanitize new name
    new_name = new_name.strip().lower()
    new_name = re.sub(r'[^a-z0-9\u4e00-\u9fff\-]', '-', new_name)
    new_name = re.sub(r'-+', '-', new_name).strip('-')

    if not new_name:
        raise HTTPException(400, "Invalid name")

    old_dir = (PAPER_NOTES_DIR / old_name).resolve()
    new_dir = (PAPER_NOTES_DIR / new_name).resolve()

    if not str(old_dir).startswith(str(PAPER_NOTES_DIR.resolve())):
        raise HTTPException(403, "Access denied")
    if not old_dir.exists():
        raise HTTPException(404, "Paper not found")
    if new_dir.exists():
        raise HTTPException(409, f"'{new_name}' already exists")

    old_dir.rename(new_dir)
    return {"ok": True, "old_name": old_name, "new_name": new_name}


@app.post("/api/download-pdf")
async def download_pdf(paper_name: str = Form(...), url: str = Form(...)):
    """Download a PDF from URL (supports arXiv abs/pdf URLs)."""
    import subprocess

    # Normalize arXiv URLs
    download_url = url.strip()
    if 'arxiv.org/abs/' in download_url:
        download_url = download_url.replace('/abs/', '/pdf/')
    if 'arxiv.org/pdf/' in download_url and not download_url.endswith('.pdf'):
        download_url += '.pdf'

    # Validate URL
    if not download_url.startswith('http'):
        raise HTTPException(400, "Invalid URL")

    # Create paper directory
    paper_dir = PAPER_NOTES_DIR / paper_name
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "images").mkdir(exist_ok=True)

    pdf_path = paper_dir / "paper.pdf"

    # Download using curl (follows redirects, handles HTTPS)
    result = subprocess.run(
        ["curl", "-L", "-f", "-o", str(pdf_path), download_url],
        capture_output=True, text=True, timeout=60,
    )

    if result.returncode != 0:
        raise HTTPException(400, f"Download failed: {result.stderr[:200]}")

    # Verify it's actually a PDF
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        with open(pdf_path, 'rb') as f:
            header = f.read(5)
        if header != b'%PDF-':
            pdf_path.unlink()
            raise HTTPException(400, "Downloaded file is not a valid PDF")
    else:
        raise HTTPException(400, "Download produced empty file")

    # Auto-extract text in background
    extracted_path = paper_dir / "extracted-text.md"
    if _extract_pdf_text_sync(pdf_path, extracted_path):
        logger.info(f"Extracted text for {paper_name}")
    else:
        logger.warning(f"Failed to extract text for {paper_name}")

    return {"ok": True, "path": str(pdf_path), "size": pdf_path.stat().st_size}


@app.post("/api/open-external")
async def open_external(paper_name: str = Form(...), file_name: str = Form(""), target: str = Form("finder")):
    """Open a file externally (Finder or IDE).

    An empty `file_name` reveals the paper's directory itself in Finder —
    useful for the "show this paper's folder" button in the preview header.
    """
    # Empty file_name → open the paper directory
    if file_name:
        target_path = (PAPER_NOTES_DIR / paper_name / file_name).resolve()
    else:
        target_path = (PAPER_NOTES_DIR / paper_name).resolve()

    if not str(target_path).startswith(str(PAPER_NOTES_DIR.resolve())):
        raise HTTPException(403, "Access denied")
    if not target_path.exists():
        raise HTTPException(404, "Not found")

    import subprocess
    import platform
    system = platform.system()
    if target == "finder":
        if system == "Darwin":
            if target_path.is_dir():
                subprocess.Popen(["open", str(target_path)])
            else:
                subprocess.Popen(["open", "-R", str(target_path)])
        elif system == "Linux":
            subprocess.Popen(["xdg-open", str(target_path)])
        else:
            subprocess.Popen(["open", str(target_path)])
    elif target == "ide":
        try:
            subprocess.Popen(["code", str(target_path)])
        except FileNotFoundError:
            if system == "Linux":
                subprocess.Popen(["xdg-open", str(target_path)])
            else:
                subprocess.Popen(["open", str(target_path)])

    return {"ok": True, "path": str(target_path)}


@app.post("/api/save-file")
async def save_file(
    paper_name: str = Form(...),
    file_name: str = Form(...),
    content: str = Form(""),
):
    """Save user-authored markdown content to paper-notes/<paper>/<file>.

    Restrictions:
    - Only `.md` files may be created/overwritten.
    - Path must resolve inside the target paper directory.
    - Refuses to overwrite canonical output files like `paper.pdf`,
      `extracted-text.md`, `speed-read.md`, `deep-learn.md`,
      `paper-reading.md`, `slides-content.md`.
    """
    import re as _re

    # Sanitize file name: only allow basenames with .md extension
    file_name = file_name.strip()
    if not file_name.endswith(".md"):
        raise HTTPException(400, "Only .md files are allowed")
    if "/" in file_name or "\\" in file_name:
        raise HTTPException(400, "Nested paths not allowed")
    if not _re.match(r"^[\w\u4e00-\u9fff.\- ]+\.md$", file_name):
        raise HTTPException(400, "Invalid file name")

    paper_dir = (PAPER_NOTES_DIR / paper_name).resolve()
    if not str(paper_dir).startswith(str(PAPER_NOTES_DIR.resolve())):
        raise HTTPException(403, "Access denied")
    if not paper_dir.exists() or not paper_dir.is_dir():
        raise HTTPException(404, "Paper not found")

    target = (paper_dir / file_name).resolve()
    if not str(target).startswith(str(paper_dir)):
        raise HTTPException(403, "Access denied")

    # Protect canonical outputs
    RESERVED = {
        "extracted-text.md",
        "speed-read.md",
        "paper-reading.md",
        "deep-learn.md",
        "slides-content.md",
    }
    if file_name in RESERVED:
        raise HTTPException(400, f"'{file_name}' is reserved; choose a different name")

    target.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(target), "size": target.stat().st_size}


@app.get("/api/files/{paper_name}/{file_path:path}")
async def get_file(paper_name: str, file_path: str):
    """Serve files from paper-notes (markdown, images, HTML)."""
    # Security: resolve and check path is within paper-notes
    target = (PAPER_NOTES_DIR / paper_name / file_path).resolve()
    if not str(target).startswith(str(PAPER_NOTES_DIR.resolve())):
        raise HTTPException(403, "Access denied")
    if not target.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(target))


@app.get("/api/file-content/{paper_name}/{file_path:path}")
async def get_file_content(paper_name: str, file_path: str):
    """Get file content as text (for markdown preview)."""
    target = (PAPER_NOTES_DIR / paper_name / file_path).resolve()
    if not str(target).startswith(str(PAPER_NOTES_DIR.resolve())):
        raise HTTPException(403, "Access denied")
    if not target.exists():
        raise HTTPException(404, "File not found")
    try:
        content = target.read_text(encoding="utf-8")
        return {"content": content, "path": str(file_path)}
    except UnicodeDecodeError:
        raise HTTPException(400, "Not a text file")


# ── Session management ────────────────────────────────────────────────

@app.post("/api/start-session")
async def start_session(
    paper_name: str = Form(...),
    mode: str = Form("speed-read"),
    pdf_url: str = Form(""),
    message: Optional[str] = Form(""),
):
    """Start a new paper-lens session using the claude CLI stdio adapter."""
    # Backup existing output files before creating new ones
    if mode != "chat":
        _backup_if_exists(paper_name, mode)

    # Build the prompt for paper-lens skill
    prompt = _build_prompt(paper_name, mode, pdf_url, message)

    adapter = KimiCLIAdapter(working_dir=str(PROJECT_DIR))
    session_id = await adapter.start(prompt)

    sessions[session_id] = (adapter, time.time())
    return {"session_id": session_id}


@app.post("/api/resume-session")
async def resume_session(
    paper_name: str = Form(...),
    claude_session_id: str = Form(...),
):
    """Restore an existing Claude session by claude_session_id.

    Used when user switches back to a paper with saved conversation history.
    The adapter is created with the claude_session_id pre-set so that
    the next send_message() call will use --resume to continue the conversation.
    """
    adapter = KimiCLIAdapter(working_dir=str(PROJECT_DIR))
    adapter.kimi_session_id = claude_session_id
    session_id = str(uuid4())
    adapter.session_id = session_id
    sessions[session_id] = (adapter, time.time())
    return {"session_id": session_id, "resumed": True}


@app.post("/api/stop-session/{session_id}")
async def stop_session(session_id: str):
    """Stop an active session and kill the underlying kimi process."""
    entry = sessions.pop(session_id, None)
    if not entry:
        raise HTTPException(404, "Session not found")

    adapter, _ = entry
    try:
        await adapter.stop()
    except Exception as e:
        logger.warning(f"Error stopping session {session_id}: {e}")

    # Also clear any pending MCP questions for this session
    mcp_pending.pop(session_id, None)

    return {"ok": True, "stopped": True}


# ── SSE streaming endpoint (replaces browser WebSocket) ───────────────

@app.get("/api/stream/{session_id}")
async def sse_stream(session_id: str):
    """Server-Sent Events endpoint for browser to receive session events."""
    entry = sessions.get(session_id)
    if not entry:
        raise HTTPException(404, "Session not found")

    adapter, _ = entry
    # Update last active
    sessions[session_id] = (adapter, time.time())

    async def event_generator():
        # Track SSE attachment so TTL cleanup doesn't reap an actively-watched session.
        if hasattr(adapter, "_subscribers"):
            adapter._subscribers += 1
        try:
            async for event in adapter.events():
                # Refresh activity timestamp on each event so a long-running
                # session's last_active doesn't age out while events are flowing.
                if session_id in sessions:
                    sessions[session_id] = (adapter, time.time())
                msg = _event_to_sse_data(event)
                if msg is not None:
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"SSE error for {session_id}: {e}")
            yield f"data: {json.dumps({'type': 'error', 'data': str(e)})}\n\n"
        finally:
            if hasattr(adapter, "_subscribers"):
                adapter._subscribers = max(0, adapter._subscribers - 1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/answer/{session_id}")
async def post_answer(session_id: str, payload: dict = Body(...)):
    """Receive user answer or message from the browser."""
    entry = sessions.get(session_id)
    if not entry:
        raise HTTPException(404, "Session not found")

    adapter, _ = entry
    sessions[session_id] = (adapter, time.time())

    msg_type = payload.get("type", "message")

    try:
        if msg_type == "answer":
            answer_text = _format_answer(payload.get("data", {}))
            # Path 1 (preferred): if an MCP `ask_user` call is parked on
            # /api/mcp/ask-user, complete its Future. The MCP server's HTTP
            # request returns to claude as tool_result, claude continues.
            mcp_entry = mcp_pending.get(session_id)
            if mcp_entry and not mcp_entry["future"].done():
                mcp_entry["future"].set_result(answer_text)
                mcp_pending.pop(session_id, None)
                logger.info(f"[MCP] answer routed to MCP waiter (session={session_id})")
                return {"ok": True}

            # Path 2 (legacy fallback): resolve a parked AskUserQuestion
            # via stdin tool_result write — works if claude actually
            # parked, which it doesn't reliably in stream-json mode.
            resolved = False
            if hasattr(adapter, "answer_question"):
                try:
                    resolved = await adapter.answer_question(answer_text)
                except Exception as e:
                    logger.warning(f"answer_question failed, falling back: {e}")
            if not resolved:
                await adapter.send_message(answer_text)
        elif msg_type == "message":
            text = payload.get("text", "")
            if text:
                await adapter.send_message(text)
        else:
            raise HTTPException(400, f"Unknown message type: {msg_type}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        raise HTTPException(500, f"发送失败: {e}")

    return {"ok": True}


# ── MCP server back-channel ───────────────────────────────────────────

@app.post("/api/mcp/ask-user")
async def mcp_ask_user(payload: dict = Body(...)):
    """Long-poll endpoint hit by `mcp_server.py` when claude calls ask_user.

    Blocks (asyncio Future) until the user submits an answer through the
    Web UI, then returns the answer text. If no one shows up, times out
    after MCP_ASK_TIMEOUT_SECONDS so the tool call surfaces a clear error
    rather than hanging the entire claude turn.
    """
    session_id = payload.get("session_id", "")
    questions = payload.get("questions", [])

    entry = sessions.get(session_id)
    if not entry:
        raise HTTPException(404, "session not found")
    adapter, _ = entry

    # If something else is already parked on this session, knock it down
    # rather than leaking the future. Shouldn't happen in normal flow.
    prev = mcp_pending.pop(session_id, None)
    if prev and not prev["future"].done():
        prev["future"].set_exception(RuntimeError("superseded by new ask_user call"))

    future: asyncio.Future = asyncio.get_event_loop().create_future()
    mcp_pending[session_id] = {"future": future, "questions": questions}

    # Push the question to the frontend through the existing event queue.
    await adapter._event_queue.put(
        SessionEvent(type=EventType.QUESTION, data=QuestionData(questions=questions))
    )
    logger.info(
        f"[MCP] ask-user blocked: session={session_id} questions={len(questions)}"
    )

    try:
        answer = await asyncio.wait_for(future, timeout=MCP_ASK_TIMEOUT_SECONDS)
        logger.info(
            f"[MCP] ask-user resolved: session={session_id} answer={answer[:80]!r}"
        )
        return {"answer": answer}
    except asyncio.TimeoutError:
        mcp_pending.pop(session_id, None)
        logger.warning(f"[MCP] ask-user timed out: session={session_id}")
        raise HTTPException(504, f"user did not answer within {MCP_ASK_TIMEOUT_SECONDS}s")


MCP_ASK_TIMEOUT_SECONDS = 1800  # 30 min — long enough for real reading sessions


# ── Helpers (unchanged) ───────────────────────────────────────────────

def _backup_if_exists(paper_name: str, mode: str) -> None:
    """Rename existing output file with version number before creating a new one.

    e.g., deep-learn.md -> deep-learn-v1.md (if first backup)
          deep-learn.md -> deep-learn-v3.md (if v1, v2 already exist)
    """
    mode_files = {
        "speed-read": "speed-read.md",
        "paper-reading": "paper-reading.md",
        "deep-learn": "deep-learn.md",
        "present": "slides-content.md",
    }
    base_name = mode_files.get(mode)
    if not base_name:
        return

    paper_dir = PAPER_NOTES_DIR / paper_name
    target = paper_dir / base_name
    if not target.exists():
        return

    stem = base_name.rsplit(".", 1)[0]  # e.g., 'deep-learn'

    # Find highest existing version number
    max_v = 0
    for f in paper_dir.glob(f"{stem}-v*.md"):
        try:
            v = int(f.stem.split("-v")[-1])
            max_v = max(max_v, v)
        except ValueError:
            pass

    next_v = max_v + 1
    versioned = paper_dir / f"{stem}-v{next_v}.md"
    target.rename(versioned)
    logger.info(f"Backed up {target.name} -> {versioned.name}")


def _build_prompt(paper_name: str, mode: str, pdf_url: str, message: str = "") -> str:
    """Build the initial prompt for paper-lens skill."""
    paper_dir = PAPER_NOTES_DIR / paper_name
    has_extracted = (paper_dir / "extracted-text.md").exists()

    if mode == "chat":
        # Free chat about a paper — don't invoke skill, just provide context
        notes = []
        for f in sorted(paper_dir.glob("*.md")):
            if f.name not in ("extracted-text.md", "README.md", "download-summary.md"):
                notes.append(f.name)
        context = f"用户正在查看论文 paper-notes/{paper_name}/。"
        if has_extracted:
            context += " 论文全文已提取到 `extracted-text.md`，请直接读取该文件了解论文内容。"
        if notes:
            context += f" 已有笔记：{', '.join(notes)}。请先读取相关笔记了解论文内容，然后回答用户的问题。"
        else:
            context += " 请根据论文内容回答用户的问题。"
        if message:
            context += f"\n\n用户的问题：{message}"
        return context

    mode_map = {
        "speed-read": "速览模式",
        "paper-reading": "论文级精读文档",
        "deep-learn": "学习模式",
        "present": "展示模式",
    }
    mode_text = mode_map.get(mode, "速览模式")

    if pdf_url:
        source = pdf_url
    else:
        # Find actual PDF file (may not be named paper.pdf)
        pdf_files = list(paper_dir.glob("*.pdf")) if paper_dir.exists() else []
        if pdf_files:
            source = str(pdf_files[0])
        else:
            source = paper_name

    # Load skill instructions from the local skill file
    skill_path = PROJECT_DIR / ".claude" / "skills" / "paper-lens"
    ref_file = skill_path / "references" / f"{mode}.md"
    skill_instructions = ""
    if ref_file.exists():
        try:
            skill_instructions = ref_file.read_text(encoding="utf-8")
        except Exception:
            pass

    prompt = (
        f"你是 Paper Lens 论文阅读助手。请阅读并分析以下论文，按照「{mode_text}」的要求输出。\n\n"
        f"论文来源：{source}\n\n"
        "【强制约束 — 必须遵守，违反会导致流程卡住】\n"
        "1. 绝对不要尝试安装任何 Python 包（如 pip install、apt install、conda install、pip3 等）。\n"
        "   系统没有 pip，任何安装命令都会失败并浪费时间。\n"
        "2. 绝对不要尝试用系统默认的 `python3` 或 `python` 运行需要 PyMuPDF 的脚本。\n"
        "   系统 Python 没有安装 pymupdf，直接运行会报 ModuleNotFoundError。\n"
        f"3. 所有需要 Python 的操作（包括读取 PDF、运行 extract_figures.py、md_to_pdf.py），"
        f"   必须使用以下路径的 Python（已预装 pymupdf/markdown/matplotlib）：\n"
        f"   {VENV_PYTHON_STR}\n"
        "   示例：{VENV_PYTHON_STR} -c \"import fitz; ...\"\n"
    )
    if has_extracted:
        prompt += (
            "4. 论文全文文本已提前提取到 `paper-notes/" + paper_name + "/extracted-text.md`。"
            "请直接读取该文件开始分析，不要再次尝试提取 PDF 文本。\n"
        )
    else:
        prompt += (
            "4. 论文全文文本尚未提取。请使用上述 Python 路径运行 PyMuPDF 提取文本，"
            "保存到 `paper-notes/" + paper_name + "/extracted-text.md`，然后读取分析。\n"
            "   不要尝试用 pdftotext、pdfplumber、pypdf 等其他工具。\n"
        )
    prompt += (
        "\n[Web UI 工具约束]\n"
        "- 凡是 skill 文档里写的 `AskUserQuestion`，本环境下一律改用 "
        "`mcp__paper_lens__ask_user`（入参 schema 完全一致：questions=[{question, header, multiSelect, options:[{label, description}]}]）。"
        "本环境的 `AskUserQuestion` 不会真的等待用户回答，会被自动跳过。\n\n"
    )
    if skill_instructions:
        prompt += f"【模式指令】\n{skill_instructions}\n\n"
    prompt += "请现在开始分析论文。"
    return prompt


def _format_answer(answer_data) -> str:
    """Format browser answer into text for Claude."""
    if isinstance(answer_data, str):
        return answer_data or "继续"
    if isinstance(answer_data, dict):
        parts = []
        for question, selections in answer_data.items():
            if isinstance(selections, list):
                parts.append(f"{question}: {', '.join(selections)}")
            else:
                parts.append(f"{question}: {selections}")
        return "\n".join(parts) if parts else "继续"
    return str(answer_data) or "继续"


def _event_to_sse_data(event: SessionEvent) -> dict | None:
    """Convert a SessionEvent to an SSE-compatible JSON dict."""
    if event.type == EventType.TEXT_DELTA:
        return {"type": "text_delta", "content": event.data}

    elif event.type == EventType.THINKING_DELTA:
        return {"type": "thinking_delta", "content": event.data}

    elif event.type == EventType.QUESTION:
        qd: QuestionData = event.data
        return {
            "type": "question",
            "questions": qd.questions,
        }

    elif event.type == EventType.FILE_SAVED:
        return {"type": "file_saved", "data": event.data}

    elif event.type == EventType.TOOL_USE:
        return {"type": "tool_use", "data": event.data}

    elif event.type == EventType.TOOL_RESULT:
        return {"type": "tool_result", "data": event.data}

    elif event.type == EventType.USAGE:
        return {"type": "usage", "data": event.data}

    elif event.type == EventType.STATUS:
        return {"type": "status", "data": event.data}

    elif event.type == EventType.ERROR:
        return {"type": "error", "data": event.data}

    elif event.type == EventType.TURN_DONE:
        return {"type": "turn_done", "data": event.data}

    elif event.type == EventType.DONE:
        return {"type": "done"}

    return None


def main():
    global SERVER_PORT
    SERVER_PORT = int(os.environ.get("PORT", 8765))
    print(f"\n  Paper-Lens Web UI v2.0")
    print(f"  http://localhost:{SERVER_PORT}\n")
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, log_level="info")


if __name__ == "__main__":
    main()
