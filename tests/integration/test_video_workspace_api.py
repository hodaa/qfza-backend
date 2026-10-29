"""Integration tests for resolving a video and preparing its transcript."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, Video
from app.services.messaging import celery_app

_TASK = "app.tasks.transcription.download_and_transcribe"
_YOUTUBE = "https://www.youtube.com/watch?v=abcdef12345"
_DRIVE = "https://drive.google.com/file/d/1DOoJhC6NHpLSu2nAvaWy3HukVimEyyhE/view"


def _video(video_id: str, *, transcribed: bool = False) -> Video:
    """Build a stored video row."""
    return Video(
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
        title="Test Video",
        duration_seconds=100,
        language="en",
        transcribed_at=datetime.now(UTC) if transcribed else None,
    )


def _record_dispatches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Capture worker task dispatches instead of publishing them."""
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, **kwargs: sent.append((name, args)),
    )
    return sent


class TestResolveVideo:
    """POST /api/videos/resolve."""

    @pytest.mark.asyncio
    async def test_creates_a_video_row_without_transcribing(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        response = await client.post("/api/videos/resolve", json={"video_url": _YOUTUBE})

        assert response.status_code == 200
        body = response.json()
        assert body["video_id"] == "abcdef12345"
        assert body["transcribed"] is False
        stored = await db_session.scalar(select(Video).where(Video.video_id == "abcdef12345"))
        assert stored is not None
        assert stored.video_url == _YOUTUBE

    @pytest.mark.asyncio
    async def test_returns_an_existing_transcribed_video(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = _video("abcdef12345", transcribed=True)
        db_session.add(video)
        await db_session.flush()

        response = await client.post("/api/videos/resolve", json={"video_url": _YOUTUBE})

        assert response.status_code == 200
        assert response.json()["id"] == str(video.id)
        assert response.json()["transcribed"] is True

    @pytest.mark.asyncio
    async def test_resolves_a_drive_link(self, client: AsyncClient) -> None:
        response = await client.post("/api/videos/resolve", json={"video_url": _DRIVE})

        assert response.status_code == 200
        assert response.json()["video_id"] == "1DOoJhC6NHpLSu2nAvaWy3HukVimEyyhE"

    @pytest.mark.asyncio
    async def test_rejects_an_unsupported_source(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/videos/resolve", json={"video_url": "https://vimeo.com/123"}
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_VIDEO_SOURCE"


class TestPrepareVideo:
    """POST /api/videos/{youtube_video_id}/prepare."""

    @pytest.mark.asyncio
    async def test_unknown_video_returns_404(self, client: AsyncClient) -> None:
        response = await client.post("/api/videos/nosuchvideo/prepare")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_ready_video_does_not_queue_or_dispatch(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("readyvideo", transcribed=True)
        db_session.add(video)
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.post("/api/videos/readyvideo/prepare")

        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "video_id": str(video.id),
            "job_id": None,
        }
        assert sent == []

    @pytest.mark.asyncio
    async def test_queues_and_dispatches_transcription(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("needsvideo")
        db_session.add(video)
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.post("/api/videos/needsvideo/prepare")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "processing"
        assert body["video_id"] == str(video.id)
        assert sent == [(_TASK, [body["job_id"]])]
        job = await db_session.scalar(select(Job).where(Job.id == body["job_id"]))
        assert job is not None
        assert job.status == "pending"

    @pytest.mark.asyncio
    async def test_reuses_an_active_job_without_dispatching(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("activevideo")
        db_session.add(video)
        await db_session.flush()
        job = Job(video_id=video.id, status="processing")
        db_session.add(job)
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.post("/api/videos/activevideo/prepare")

        assert response.status_code == 200
        assert response.json()["job_id"] == str(job.id)
        assert sent == []
