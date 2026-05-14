"""Tests for Paper-Lens Web UI server endpoints."""
from __future__ import annotations

import json
import sys
import os
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from server import app, PAPER_NOTES_DIR


client = TestClient(app)


class TestIndex:
    def test_returns_service_info(self):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "paper-lens-backend"
        assert "frontend" in data


class TestPaperList:
    def test_returns_papers(self):
        resp = client.get("/api/papers")
        assert resp.status_code == 200
        data = resp.json()
        assert "papers" in data
        assert isinstance(data["papers"], list)

    def test_paper_has_expected_fields(self):
        resp = client.get("/api/papers")
        data = resp.json()
        if data["papers"]:
            paper = data["papers"][0]
            assert "name" in paper
            assert "files" in paper
            assert "has_speed_read" in paper
            assert "has_paper_reading" in paper
            assert "has_deep_learn" in paper
            assert "has_slides" in paper
            assert "has_presentation" in paper
            assert "has_pdf" in paper

    def test_presentation_detected(self):
        """Papers with .html files should have has_presentation=True."""
        resp = client.get("/api/papers")
        data = resp.json()
        for paper in data["papers"]:
            html_files = [f for f in paper["files"] if f.endswith(".html")]
            assert paper["has_presentation"] == (len(html_files) > 0)
            if paper["has_presentation"]:
                assert paper["presentation_file"] is not None
                assert paper["presentation_file"].endswith(".html")

    def test_paper_reading_detected_and_sorted(self):
        """paper-reading.md should be detected and sorted after speed-read."""
        import shutil

        test_dir = PAPER_NOTES_DIR / "test-paper-reading-mode"
        shutil.rmtree(test_dir, ignore_errors=True)
        test_dir.mkdir(parents=True)
        try:
            for name in [
                "paper.pdf",
                "slides-content.md",
                "deep-learn.md",
                "paper-reading.md",
                "speed-read.md",
                "extracted-text.md",
            ]:
                (test_dir / name).write_text("x", encoding="utf-8")

            resp = client.get("/api/papers")
            assert resp.status_code == 200
            paper = next(p for p in resp.json()["papers"] if p["name"] == test_dir.name)

            assert paper["has_paper_reading"] is True
            assert "paper-reading.md" in paper["note_files"]
            assert "extracted-text.md" not in paper["note_files"]
            assert paper["note_files"][:4] == [
                "speed-read.md",
                "paper-reading.md",
                "deep-learn.md",
                "slides-content.md",
            ]
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


class TestPaperReadingMode:
    def test_backup_if_exists_versions_paper_reading(self):
        """paper-reading.md should be backed up with versioned names."""
        import shutil
        from server import _backup_if_exists

        test_dir = PAPER_NOTES_DIR / "test-paper-reading-backup"
        shutil.rmtree(test_dir, ignore_errors=True)
        test_dir.mkdir(parents=True)
        try:
            (test_dir / "paper-reading.md").write_text("first", encoding="utf-8")
            _backup_if_exists(test_dir.name, "paper-reading")
            assert not (test_dir / "paper-reading.md").exists()
            assert (test_dir / "paper-reading-v1.md").read_text(encoding="utf-8") == "first"

            (test_dir / "paper-reading.md").write_text("second", encoding="utf-8")
            _backup_if_exists(test_dir.name, "paper-reading")
            assert (test_dir / "paper-reading-v2.md").read_text(encoding="utf-8") == "second"
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_build_prompt_uses_paper_reading_mode_label(self):
        """paper-reading mode should ask the skill for 论文级精读文档."""
        import shutil
        from server import _build_prompt

        test_dir = PAPER_NOTES_DIR / "test-paper-reading-prompt"
        shutil.rmtree(test_dir, ignore_errors=True)
        test_dir.mkdir(parents=True)
        try:
            (test_dir / "paper.pdf").write_bytes(b"%PDF-1.0\n")
            prompt = _build_prompt(test_dir.name, "paper-reading", "")
            assert "/paper-lens" in prompt
            assert "选择：论文级精读文档" in prompt
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


class TestFileAccess:
    def test_rejects_path_traversal(self):
        resp = client.get("/api/files/../../etc/passwd")
        assert resp.status_code in (403, 404, 422)

    def test_returns_404_for_missing(self):
        resp = client.get("/api/files/nonexistent/foo.md")
        assert resp.status_code == 404

    def test_file_content_endpoint(self):
        # Find an actual paper with files
        papers_resp = client.get("/api/papers")
        papers = papers_resp.json()["papers"]
        for p in papers:
            note_files = p.get("note_files", [])
            if note_files:
                resp = client.get(f"/api/file-content/{p['name']}/{note_files[0]}")
                assert resp.status_code == 200
                data = resp.json()
                assert "content" in data
                assert len(data["content"]) > 0
                break


class TestPaperDetail:
    """Test the paper detail endpoint that returns all file info for preview loading."""

    def test_paper_detail_returns_files_with_metadata(self):
        resp = client.get("/api/papers")
        papers = resp.json()["papers"]
        if papers:
            name = papers[0]["name"]
            resp = client.get(f"/api/paper/{name}")
            assert resp.status_code == 200
            data = resp.json()
            assert "name" in data
            assert "files" in data
            # Files should include size and mtime for sorting
            if data["files"]:
                f = data["files"][0]
                assert "name" in f
                assert "size" in f


class TestUpload:
    def test_rejects_non_pdf(self):
        resp = client.post(
            "/api/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )
        assert resp.status_code == 400

    def test_accepts_pdf(self):
        # Minimal PDF content
        pdf_bytes = b"%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n"
        resp = client.post(
            "/api/upload",
            files={"file": ("test-upload.pdf", pdf_bytes, "application/pdf")},
            data={"name": "__test-paper__"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # underscores get normalized to hyphens
        assert data["paper_name"] == "--test-paper--"

        # Cleanup
        import shutil
        test_dir = PAPER_NOTES_DIR / "--test-paper--"
        if test_dir.exists():
            shutil.rmtree(test_dir)


class TestRenamePaper:
    def test_rename_success(self):
        """Create a temp paper, rename it, verify."""
        import shutil
        test_dir = PAPER_NOTES_DIR / "__test-rename-old__"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "paper.pdf").write_bytes(b"%PDF-1.0\n")

        try:
            resp = client.post(
                "/api/rename-paper",
                data={"old_name": "__test-rename-old__", "new_name": "test-rename-new"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["new_name"] == "test-rename-new"
            assert (PAPER_NOTES_DIR / "test-rename-new").exists()
            assert not (PAPER_NOTES_DIR / "__test-rename-old__").exists()
        finally:
            shutil.rmtree(PAPER_NOTES_DIR / "test-rename-new", ignore_errors=True)
            shutil.rmtree(PAPER_NOTES_DIR / "__test-rename-old__", ignore_errors=True)

    def test_rename_conflict(self):
        """Cannot rename to existing directory."""
        resp = client.get("/api/papers")
        papers = resp.json()["papers"]
        if len(papers) >= 2:
            resp = client.post(
                "/api/rename-paper",
                data={"old_name": papers[0]["name"], "new_name": papers[1]["name"]},
            )
            assert resp.status_code == 409

    def test_rename_nonexistent(self):
        resp = client.post(
            "/api/rename-paper",
            data={"old_name": "__nonexistent__", "new_name": "something"},
        )
        assert resp.status_code == 404


class TestDownloadPdf:
    def test_rejects_invalid_url(self):
        resp = client.post(
            "/api/download-pdf",
            data={"paper_name": "__test__", "url": "not-a-url"},
        )
        assert resp.status_code == 400

    def test_rejects_bad_url(self):
        resp = client.post(
            "/api/download-pdf",
            data={"paper_name": "__test__", "url": "https://httpbin.org/status/404"},
        )
        assert resp.status_code == 400


class TestBatchFolder:
    def test_batch_folder_has_no_pdf(self):
        """batch-* folders should not be treated as papers with PDF."""
        resp = client.get("/api/papers")
        for paper in resp.json()["papers"]:
            if paper["name"].startswith("batch-"):
                # batch folders typically don't have paper.pdf
                assert "download-summary.md" in paper["files"]


# ── New v2.0 tests ────────────────────────────────────────────────────

class TestSSEEndpoint:
    def test_returns_correct_content_type(self):
        """SSE endpoint should return text/event-stream for a valid session."""
        # We need a mock adapter in the sessions dict
        from server import sessions
        from adapters.claude_cli import ClaudeCLIAdapter
        from adapters.base import SessionEvent, EventType
        import asyncio
        import time

        # Create a mock adapter that yields one DONE event
        adapter = MagicMock(spec=ClaudeCLIAdapter)

        async def mock_events():
            yield SessionEvent(type=EventType.DONE)

        adapter.events = mock_events

        test_sid = "__test-sse-session__"
        sessions[test_sid] = (adapter, time.time())

        try:
            # Use stream=True to avoid reading forever (SSE is a streaming response)
            with client.stream("GET", f"/api/stream/{test_sid}") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                # Read the first chunk
                lines = []
                for chunk in resp.iter_text():
                    lines.append(chunk)
                    break  # just check first chunk
        finally:
            sessions.pop(test_sid, None)

    def test_returns_404_for_unknown_session(self):
        resp = client.get("/api/stream/__nonexistent__")
        assert resp.status_code == 404


class TestAnswerEndpoint:
    def test_returns_404_for_unknown_session(self):
        resp = client.post(
            "/api/answer/__nonexistent__",
            json={"type": "message", "text": "hello"},
        )
        assert resp.status_code == 404

    def test_accepts_answer_for_valid_session(self):
        """POST /api/answer should accept messages for a valid session."""
        from server import sessions
        from adapters.claude_cli import ClaudeCLIAdapter
        import asyncio
        import time

        adapter = MagicMock(spec=ClaudeCLIAdapter)
        adapter.send_message = AsyncMock()

        test_sid = "__test-answer-session__"
        sessions[test_sid] = (adapter, time.time())

        try:
            resp = client.post(
                f"/api/answer/{test_sid}",
                json={"type": "message", "text": "hello"},
            )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
            adapter.send_message.assert_called_once_with("hello")
        finally:
            sessions.pop(test_sid, None)

    def test_accepts_structured_answer(self):
        """POST /api/answer with type=answer should format answer data."""
        from server import sessions
        from adapters.claude_cli import ClaudeCLIAdapter
        import time

        adapter = MagicMock(spec=ClaudeCLIAdapter)
        adapter.send_message = AsyncMock()
        adapter.answer_question = AsyncMock(return_value=False)

        test_sid = "__test-answer-structured__"
        sessions[test_sid] = (adapter, time.time())

        try:
            resp = client.post(
                f"/api/answer/{test_sid}",
                json={
                    "type": "answer",
                    "data": {"选择分析深度": ["详细分析", "包含代码"]},
                },
            )
            assert resp.status_code == 200
            # Check the formatted answer was sent
            call_args = adapter.send_message.call_args[0][0]
            assert "详细分析" in call_args
            assert "包含代码" in call_args
        finally:
            sessions.pop(test_sid, None)


class TestExistingEndpointsStillWork:
    """Verify that all pre-existing REST endpoints are unbroken after v2.0 rewrite."""

    def test_index_still_serves(self):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_papers_endpoint(self):
        resp = client.get("/api/papers")
        assert resp.status_code == 200
        assert "papers" in resp.json()

    def test_file_404(self):
        resp = client.get("/api/files/nonexistent/foo.md")
        assert resp.status_code == 404

    def test_file_content_404(self):
        resp = client.get("/api/file-content/nonexistent/foo.md")
        assert resp.status_code == 404

    def test_upload_rejects_non_pdf(self):
        resp = client.post(
            "/api/upload",
            files={"file": ("bad.txt", b"not pdf", "text/plain")},
        )
        assert resp.status_code == 400

    def test_rename_404(self):
        resp = client.post(
            "/api/rename-paper",
            data={"old_name": "__gone__", "new_name": "x"},
        )
        assert resp.status_code == 404

    def test_download_invalid_url(self):
        resp = client.post(
            "/api/download-pdf",
            data={"paper_name": "__test__", "url": "ftp://bad"},
        )
        assert resp.status_code == 400
