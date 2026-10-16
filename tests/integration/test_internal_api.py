"""Integration tests for the internal worker API endpoints."""

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Job, Video, VideoDownload
from app.schemas import JobStatus

settings = get_settings()

_INTERNAL_KEY = settings.internal_api_key


def _headers() -> dict:
    """Build internal API auth headers for a tested target."""
    return {"X-Internal-API-Key": _INTERNAL_KEY}


async def _create_job_with_video(
    db_session: AsyncSession, *, status: JobStatus = JobStatus.PENDING
) -> tuple[Video, Job]:
    """Create a video with a unique URL plus a linked job, returning both."""
    uid = uuid4().hex[:12]
    video = Video(
        youtube_url=f"https://www.youtube.com/watch?v={uid}",
        video_id=uid,
        language="en",
    )
    db_session.add(video)
    await db_session.flush()

    job = Job(video_id=video.id, status=status.value)
    db_session.add(job)
    await db_session.flush()
    await db_session.refresh(video)
    await db_session.refresh(job)
    return video, job


class TestInternalAuth:
    """Tests for internal API key enforcement."""

    @pytest.mark.asyncio
    async def test_rejects_missing_key(self, client: AsyncClient) -> None:
        response = await client.get("/internal/jobs/00000000-0000-0000-0000-000000000000")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_wrong_key(self, client: AsyncClient) -> None:
        response = await client.get(
            "/internal/jobs/00000000-0000-0000-0000-000000000000",
            headers={"X-Internal-API-Key": "wrong"},
        )

        assert response.status_code == 401


class TestInternalGetJob:
    """Tests for GET /internal/jobs/{job_id}."""

    @pytest.mark.asyncio
    async def test_returns_job_and_video_data(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video, job = await _create_job_with_video(db_session)

        response = await client.get(f"/internal/jobs/{job.id}", headers=_headers())

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == str(job.id)
        assert data["video_id"] == str(video.id)
        assert data["youtube_video_id"] == video.video_id
        assert data["youtube_url"] == video.youtube_url
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_job(self, client: AsyncClient) -> None:
        response = await client.get(
            "/internal/jobs/00000000-0000-0000-0000-000000000000", headers=_headers()
        )

        assert response.status_code == 404


class TestInternalLifecycle:
    """Tests for job lifecycle mutation endpoints."""

    @pytest.mark.asyncio
    async def test_advance_complete_fail_flow(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        _, job = await _create_job_with_video(db_session)

        advance = await client.post(f"/internal/jobs/{job.id}/advance", headers=_headers())
        assert advance.status_code == 200
        assert advance.json()["status"] == "processing"

        complete = await client.post(f"/internal/jobs/{job.id}/complete", headers=_headers())
        assert complete.status_code == 200
        assert complete.json()["status"] == "completed"

        fail = await client.post(
            f"/internal/jobs/{job.id}/fail",
            json={"error": "Something went wrong"},
            headers=_headers(),
        )
        assert fail.status_code == 200
        assert fail.json()["status"] == "failed"

    @pytest.mark.asyncio
    async def test_complete_with_message_stores_error_note(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        _, job = await _create_job_with_video(db_session)

        complete = await client.post(
            f"/internal/jobs/{job.id}/complete",
            json={"message": "No speech detected in this video."},
            headers=_headers(),
        )
        assert complete.status_code == 200
        assert complete.json()["status"] == "completed"

        status = await client.get(f"/api/status/{job.id}", headers=_headers())
        assert status.status_code == 200
        assert status.json()["status"] == "completed"
        assert status.json()["error"] == "No speech detected in this video."

    @pytest.mark.asyncio
    async def test_store_transcript_updates_video(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video, job = await _create_job_with_video(db_session, status=JobStatus.PROCESSING)

        response = await client.post(
            f"/internal/jobs/{job.id}/transcript",
            json={
                "title": "Seeded Video Title",
                "duration_seconds": 120,
                "language": "en",
                "transcript_text": "hello world",
                "provider": "supadata",
                "words": [
                    {"word_index": 0, "word": "hello", "start_time": 0.0, "end_time": 0.5},
                    {"word_index": 1, "word": "world", "start_time": 0.5, "end_time": 1.0},
                ],
            },
            headers=_headers(),
        )

        assert response.status_code == 200

        await db_session.refresh(video)
        assert video.title == "Seeded Video Title"
        assert video.duration_seconds == 120
        assert video.language == "en"
        assert video.transcript == "hello world"
        assert video.provider == "supadata"
        assert video.transcribed_at is not None

    @pytest.mark.asyncio
    async def test_store_no_speech_transcript_stores_null(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """An empty transcript is accepted (no 422) and stored as a NULL row:
        the video is transcribed-but-silent, so it never re-queues a job yet
        has no searchable content."""
        video, job = await _create_job_with_video(db_session, status=JobStatus.PROCESSING)

        response = await client.post(
            f"/internal/jobs/{job.id}/transcript",
            json={
                "title": "Silent Video",
                "duration_seconds": 60,
                "language": "ar",
                "transcript_text": "",
                "provider": "yt-dlp",
                "words": [],
            },
            headers=_headers(),
        )

        assert response.status_code == 200

        await db_session.refresh(video)
        assert video.transcript is None
        assert video.transcript_tsvector is None
        assert video.transcribed_at is not None

    @pytest.mark.asyncio
    async def test_store_transcript_idempotent_on_retry(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """A second store for the same video replaces prior words instead of 500ing."""
        video, job = await _create_job_with_video(db_session, status=JobStatus.PROCESSING)
        payload = {
            "title": "Video Title",
            "duration_seconds": 120,
            "language": "en",
            "transcript_text": "hello world lorem ipsum",
            "provider": "yt-dlp",
            "words": [
                {"word_index": 0, "word": "hello", "start_time": 0.0, "end_time": 0.5},
                {"word_index": 1, "word": "world", "start_time": 0.5, "end_time": 1.0},
            ],
        }

        first = await client.post(
            f"/internal/jobs/{job.id}/transcript", json=payload, headers=_headers()
        )
        assert first.status_code == 200

        payload["words"][0]["word"] = "hi"
        second = await client.post(
            f"/internal/jobs/{job.id}/transcript", json=payload, headers=_headers()
        )
        assert second.status_code == 200

        result = await db_session.execute(
            text("SELECT word FROM transcript_words WHERE video_id = :vid ORDER BY word_index"),
            {"vid": video.id},
        )
        assert result.scalars().all() == ["hi", "world"]

    @pytest.mark.asyncio
    async def test_complete_missing_job_returns_404(self, client: AsyncClient) -> None:
        response = await client.post(
            "/internal/jobs/00000000-0000-0000-0000-000000000000/complete", headers=_headers()
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_fail_missing_job_returns_404(self, client: AsyncClient) -> None:
        response = await client.post(
            "/internal/jobs/00000000-0000-0000-0000-000000000000/fail",
            json={"error": "Something went wrong"},
            headers=_headers(),
        )
        assert response.status_code == 404


class TestInternalProgress:
    """Tests for the incremental progress reporting endpoint."""

    @pytest.mark.asyncio
    async def test_updates_progress_without_changing_status(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        _, job = await _create_job_with_video(db_session, status=JobStatus.PENDING)

        response = await client.post(
            f"/internal/jobs/{job.id}/progress", json={"progress": 40}, headers=_headers()
        )

        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        await db_session.refresh(job)
        assert job.progress == 40

    @pytest.mark.asyncio
    async def test_missing_job_returns_404(self, client: AsyncClient) -> None:
        response = await client.post(
            "/internal/jobs/00000000-0000-0000-0000-000000000000/progress",
            json={"progress": 40},
            headers=_headers(),
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_rejects_out_of_range_progress(self, client: AsyncClient) -> None:
        response = await client.post(
            "/internal/jobs/00000000-0000-0000-0000-000000000000/progress",
            json={"progress": 150},
            headers=_headers(),
        )
        assert response.status_code == 422


class _BlobRecorder:
    """Async blob deleter that records calls."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(self, blob_url: str) -> None:
        self.urls.append(blob_url)


async def _seed_download(
    db_session: AsyncSession,
    video: Video,
    *,
    status: str = "pending",
    blob_url: str | None = None,
    start: int | None = None,
    end: int | None = None,
    height: int | None = None,
) -> VideoDownload:
    """Store a download row for the worker-facing endpoints."""
    download = VideoDownload(
        video_id=video.id,
        status=status,
        blob_url=blob_url,
        start_seconds=start,
        end_seconds=end,
        height=height,
    )
    db_session.add(download)
    await db_session.flush()
    await db_session.refresh(download)
    return download


class TestInternalDownloads:
    """Tests for the worker-facing download endpoints."""

    @pytest.mark.asyncio
    async def test_get_returns_the_worker_payload(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video, _ = await _create_job_with_video(db_session)
        download = await _seed_download(db_session, video, start=30, end=90, height=360)

        response = await client.get(f"/internal/downloads/{download.id}", headers=_headers())

        assert response.status_code == 200
        data = response.json()
        assert data["download_id"] == str(download.id)
        assert data["video_id"] == str(video.id)
        assert data["youtube_video_id"] == video.video_id
        assert data["youtube_url"] == video.youtube_url
        assert data["status"] == "pending"
        assert data["start_seconds"] == 30
        assert data["end_seconds"] == 90
        assert data["height"] == 360

    @pytest.mark.asyncio
    async def test_missing_download_returns_404(self, client: AsyncClient) -> None:
        response = await client.get(
            "/internal/downloads/00000000-0000-0000-0000-000000000000", headers=_headers()
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_lifecycle_advance_ready_failed(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video, _ = await _create_job_with_video(db_session)
        download = await _seed_download(db_session, video)

        advance = await client.post(
            f"/internal/downloads/{download.id}/advance", headers=_headers()
        )
        assert advance.json()["status"] == "processing"

        ready = await client.post(
            f"/internal/downloads/{download.id}/ready",
            json={"blob_url": "https://blob/file"},
            headers=_headers(),
        )
        assert ready.json()["status"] == "ready"

        failed = await client.post(
            f"/internal/downloads/{download.id}/failed",
            json={"error": "The download failed"},
            headers=_headers(),
        )
        assert failed.json()["status"] == "failed"

        status = await client.get(f"/api/videos/{video.video_id}/downloads/{download.id}")
        assert status.json()["status"] == "failed"
        assert status.json()["error"] == "The download failed"

    @pytest.mark.asyncio
    async def test_ready_for_a_consumed_row_discards_the_new_blob(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video, _ = await _create_job_with_video(db_session)
        download = await _seed_download(
            db_session, video, status="consumed", blob_url="https://blob/old"
        )
        blob = _BlobRecorder()
        monkeypatch.setattr("app.services.video_downloads.delete_blob_url", blob)

        ready = await client.post(
            f"/internal/downloads/{download.id}/ready",
            json={"blob_url": "https://blob/late"},
            headers=_headers(),
        )

        assert ready.status_code == 200
        assert ready.json()["status"] == "consumed"
        assert blob.urls == ["https://blob/late"]

    @pytest.mark.asyncio
    async def test_download_routes_require_the_key(self, client: AsyncClient) -> None:
        response = await client.get("/internal/downloads/00000000-0000-0000-0000-000000000000")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_formats_endpoint_caches_the_ladder(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video, _ = await _create_job_with_video(db_session)

        stored = await client.post(
            f"/internal/videos/{video.video_id}/formats",
            json={"heights": [1080, 720]},
            headers=_headers(),
        )
        assert stored.status_code == 200

        await db_session.refresh(video)
        assert video.available_heights == [1080, 720]
        assert video.formats_refreshed_at is not None

        listed = await client.get(f"/api/videos/{video.video_id}/formats")
        assert listed.json() == {"heights": [1080, 720]}

    @pytest.mark.asyncio
    async def test_formats_endpoint_404_for_unknown_video(self, client: AsyncClient) -> None:
        response = await client.post(
            "/internal/videos/nosuchvideo/formats",
            json={"heights": []},
            headers=_headers(),
        )

        assert response.status_code == 404
