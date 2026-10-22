"""Integration tests for the Assembly completion webhook endpoint."""

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, Video


async def _create_job_with_video(
    db_session: AsyncSession, *, status: str = "processing"
) -> tuple[Video, Job]:
    """Create a video with a unique URL plus a linked job, returning both."""
    uid = uuid4().hex[:12]
    video = Video(
        video_url=f"https://www.youtube.com/watch?v={uid}",
        video_id=uid,
        language="en",
    )
    db_session.add(video)
    await db_session.flush()

    job = Job(video_id=video.id, status=status)
    db_session.add(job)
    await db_session.flush()
    await db_session.refresh(video)
    await db_session.refresh(job)
    return video, job


_WEBHOOK_URL = "/api/webhooks/assembly"


def _webhook_path(job_id, provider: str = "yt-dlp") -> str:
    return f"{_WEBHOOK_URL}?job_id={job_id}&provider={provider}"


class TestWebhookRecipient:
    """Tests for the filled-in POST body being accepted."""

    @pytest.mark.asyncio
    async def test_requires_transcript_id(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        _, job = await _create_job_with_video(db_session, status="processing")

        response = await client.post(
            _webhook_path(job.id),
            json={"transcript_id": "", "status": "completed"},
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_requires_valid_status(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        _, job = await _create_job_with_video(db_session, status="processing")

        response = await client.post(
            _webhook_path(job.id),
            json={"transcript_id": "asm-123", "status": "queued"},
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_requires_valid_job_id_query(self, client: AsyncClient) -> None:
        response = await client.post(
            f"{_WEBHOOK_URL}?job_id=not-a-uuid&provider=yt-dlp",
            json={"transcript_id": "asm-123", "status": "completed"},
        )

        assert response.status_code == 422


class TestCompletedWebhook:
    """Tests for the completed status path."""

    @pytest.mark.asyncio
    async def test_reenqueues_and_leaves_job_processing(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ) -> None:
        from app.services import webhooks

        _, job = await _create_job_with_video(db_session, status="processing")
        dispatched: list = []
        monkeypatch.setattr(
            webhooks,
            "dispatch_resume_transcription",
            lambda job_id, transcript_id, provider: dispatched.append(
                (job_id, transcript_id, provider)
            ),
        )

        response = await client.post(
            _webhook_path(job.id),
            json={"transcript_id": "asm-123", "status": "completed"},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert dispatched == [(job.id, "asm-123", "yt-dlp")]
        await db_session.refresh(job)
        assert job.status == "processing"

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_job(self, client: AsyncClient) -> None:
        response = await client.post(
            f"{_WEBHOOK_URL}?job_id=00000000-0000-0000-0000-000000000000&provider=yt-dlp",
            json={"transcript_id": "asm-123", "status": "completed"},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_terminal_job_is_left_untouched(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ) -> None:
        from app.services import webhooks

        _, job = await _create_job_with_video(db_session, status="completed")
        dispatched: list = []
        monkeypatch.setattr(
            webhooks,
            "dispatch_resume_transcription",
            lambda job_id, transcript_id, provider: dispatched.append(
                (job_id, transcript_id, provider)
            ),
        )

        response = await client.post(
            _webhook_path(job.id),
            json={"transcript_id": "asm-123", "status": "completed"},
        )

        assert response.status_code == 200
        assert dispatched == []
        await db_session.refresh(job)
        assert job.status == "completed"


class TestErrorWebhook:
    """Tests for the error status path."""

    @pytest.mark.asyncio
    async def test_fails_job(self, client: AsyncClient, db_session: AsyncSession) -> None:
        _, job = await _create_job_with_video(db_session, status="processing")

        response = await client.post(
            _webhook_path(job.id),
            json={"transcript_id": "asm-123", "status": "error"},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        await db_session.refresh(job)
        assert job.status == "failed"
        assert job.error is not None
