"""Integration tests for the lecture-notes API and worker callbacks."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import TranscriptWord, Video, VideoNote
from app.services.messaging import celery_app

settings = get_settings()
_HEADERS = {"X-Internal-API-Key": settings.internal_api_key}
_TASK = "app.tasks.notes.generate_lecture_notes"


def _video(video_id: str, *, transcribed: bool = True) -> Video:
    """Build a stored video row."""
    return Video(
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
        title="Test Video",
        duration_seconds=100,
        language="en",
        transcribed_at=datetime.now(UTC) if transcribed else None,
    )


def _note(video: Video, *, status: str = "pending", blob_url: str | None = None) -> VideoNote:
    """Build a lecture-notes row for a stored video."""
    return VideoNote(video_id=video.id, status=status, blob_url=blob_url)


def _record_dispatches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Capture worker task dispatches instead of publishing them."""
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, **kwargs: sent.append((name, args)),
    )
    return sent


async def _add_words(db_session: AsyncSession, video: Video) -> None:
    """Add a short transcript to a video."""
    for index, (word, start, end) in enumerate([("Hello", 0.0, 0.4), ("world", 0.4, 0.8)]):
        db_session.add(
            TranscriptWord(
                video_id=video.id,
                word_index=index,
                word=word,
                start_time=start,
                end_time=end,
            )
        )
    await db_session.flush()


class TestCreateVideoNotes:
    """POST /api/videos/{youtube_video_id}/notes."""

    @pytest.mark.asyncio
    async def test_unknown_video_returns_404(self, client: AsyncClient) -> None:
        response = await client.post("/api/videos/nosuchvideo/notes")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_untranscribed_video_returns_404(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        db_session.add(_video("notespending", transcribed=False))
        await db_session.flush()

        response = await client.post("/api/videos/notespending/notes")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "VIDEO_NOT_TRANSCRIBED"

    @pytest.mark.asyncio
    async def test_queues_and_dispatches_a_new_request(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(_video("notesnew"))
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.post("/api/videos/notesnew/notes")

        assert response.status_code == 202
        assert response.json()["status"] == "pending"
        assert sent == [(_TASK, [response.json()["id"]])]

    @pytest.mark.asyncio
    async def test_reuses_an_active_request_without_dispatching(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("notesactive")
        db_session.add(video)
        await db_session.flush()
        note = _note(video, status="processing")
        db_session.add(note)
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.post("/api/videos/notesactive/notes")

        assert response.status_code == 202
        assert response.json() == {"id": str(note.id), "status": "processing"}
        assert sent == []

    @pytest.mark.asyncio
    async def test_reuses_a_ready_pdf_without_dispatching(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("notesready")
        db_session.add(video)
        await db_session.flush()
        note = _note(video, status="ready", blob_url="https://blob/notes.pdf")
        db_session.add(note)
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.post("/api/videos/notesready/notes")

        assert response.status_code == 202
        assert response.json()["status"] == "ready"
        assert sent == []

    @pytest.mark.asyncio
    async def test_regenerate_retires_the_cached_pdf_and_queues_again(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("notesregenerate")
        db_session.add(video)
        await db_session.flush()
        cached = _note(video, status="ready", blob_url="https://blob/old.pdf")
        db_session.add(cached)
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.post("/api/videos/notesregenerate/notes?regenerate=true")

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        assert body["id"] != str(cached.id)
        assert sent == [(_TASK, [body["id"]])]
        await db_session.refresh(cached)
        assert cached.status == "failed"

    @pytest.mark.asyncio
    async def test_expires_a_stale_request_and_queues_a_fresh_one(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = _video("notesstale")
        db_session.add(video)
        await db_session.flush()
        stale = _note(video, status="processing")
        stale.updated_at = datetime.now(UTC) - timedelta(hours=2)
        db_session.add(stale)
        await db_session.flush()

        response = await client.post("/api/videos/notesstale/notes")

        assert response.status_code == 202
        assert response.json()["status"] == "pending"
        await db_session.refresh(stale)
        assert stale.status == "failed"


class TestGetVideoNotes:
    """GET /api/videos/{youtube_video_id}/notes/{note_id}."""

    @pytest.mark.asyncio
    async def test_unknown_video_returns_404(self, client: AsyncClient) -> None:
        note_id = uuid4()
        response = await client.get(f"/api/videos/nosuchvideo/notes/{note_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_unknown_note_returns_404(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        db_session.add(_video("notesmissing"))
        await db_session.flush()

        response = await client.get(f"/api/videos/notesmissing/notes/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_note_from_another_video_returns_404(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        first = _video("notesfirst")
        second = _video("notessecond")
        db_session.add_all([first, second])
        await db_session.flush()
        note = _note(first, status="processing")
        db_session.add(note)
        await db_session.flush()

        response = await client.get(f"/api/videos/notessecond/notes/{note.id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_ready_note_returns_url_and_filename(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = _video("notesurl")
        db_session.add(video)
        await db_session.flush()
        note = _note(video, status="ready", blob_url="https://blob/notes.pdf")
        db_session.add(note)
        await db_session.flush()

        response = await client.get(f"/api/videos/notesurl/notes/{note.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["url"] == "https://blob/notes.pdf"
        assert data["filename"] == "notes-notesurl.pdf"

    @pytest.mark.asyncio
    async def test_processing_note_hides_url_and_error(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = _video("notesprocessing")
        db_session.add(video)
        await db_session.flush()
        note = _note(video, status="processing")
        db_session.add(note)
        await db_session.flush()

        response = await client.get(f"/api/videos/notesprocessing/notes/{note.id}")

        assert response.status_code == 200
        assert response.json()["url"] is None
        assert response.json()["error"] is None

    @pytest.mark.asyncio
    async def test_malformed_note_id_returns_422(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        db_session.add(_video("notesbadid"))
        await db_session.flush()

        response = await client.get("/api/videos/notesbadid/notes/not-a-uuid")

        assert response.status_code == 422


class TestInternalNotes:
    """The worker-facing lecture-notes endpoints."""

    @pytest.mark.asyncio
    async def test_get_requires_internal_key(self, client: AsyncClient) -> None:
        response = await client.get(f"/internal/notes/{uuid4()}")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_returns_video_and_transcript_lines(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = _video("notesinternal")
        db_session.add(video)
        await db_session.flush()
        await _add_words(db_session, video)
        note = _note(video)
        db_session.add(note)
        await db_session.flush()

        response = await client.get(f"/internal/notes/{note.id}", headers=_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["note_id"] == str(note.id)
        assert data["youtube_url"] == video.video_url
        assert data["transcript_lines"] == [{"seconds": 0.0, "text": "Hello world"}]

    @pytest.mark.asyncio
    async def test_missing_note_returns_404(self, client: AsyncClient) -> None:
        response = await client.get(f"/internal/notes/{uuid4()}", headers=_HEADERS)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_advance_ready_and_failed_callbacks(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = _video("notescallbacks")
        db_session.add(video)
        await db_session.flush()
        note = _note(video)
        db_session.add(note)
        await db_session.flush()

        advanced = await client.post(f"/internal/notes/{note.id}/advance", headers=_HEADERS)
        ready = await client.post(
            f"/internal/notes/{note.id}/ready",
            headers=_HEADERS,
            json={"blob_url": "https://blob/notes.pdf"},
        )
        failed = await client.post(
            f"/internal/notes/{note.id}/failed",
            headers=_HEADERS,
            json={"error": "generation failed"},
        )

        assert advanced.json() == {"status": "processing"}
        assert ready.json() == {"status": "ready"}
        assert failed.json() == {"status": "failed"}
        await db_session.refresh(note)
        assert note.error == "generation failed"

    @pytest.mark.asyncio
    async def test_callback_for_missing_note_returns_404(self, client: AsyncClient) -> None:
        response = await client.post(
            f"/internal/notes/{uuid4()}/advance",
            headers=_HEADERS,
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
