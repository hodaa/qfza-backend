"""Integration tests for the video download endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Video, VideoDownload
from app.services.messaging import celery_app
from app.services.video_downloads import (
    STATUS_CONSUMED,
    STATUS_PENDING,
    STATUS_READY,
    SWEEP_FAILURE_MESSAGE,
)


def _video(video_id: str) -> Video:
    """Build a stored video row."""
    return Video(
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
        title="Test Video",
        duration_seconds=100,
        language="en",
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


class BlobRecorder:
    """Async blob deleter that records calls and can be made to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.urls: list[str] = []
        self.fail = fail

    async def __call__(self, blob_url: str) -> None:
        self.urls.append(blob_url)
        if self.fail:
            raise RuntimeError("blob store is down")


async def _seed_download(
    db_session: AsyncSession,
    video: Video,
    *,
    status: str = STATUS_PENDING,
    blob_url: str | None = None,
    start: int | None = None,
    end: int | None = None,
    height: int | None = None,
) -> VideoDownload:
    """Store a download row directly, bypassing the queue route."""
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


class TestVideoFormats:
    """The formats endpoint, served from the worker-probed cache."""

    async def test_unknown_video_returns_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/videos/nosuchvideo/formats")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    async def test_serves_a_cached_ladder_without_dispatching(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("vidformats1")
        video.available_heights = [1080, 720]
        video.formats_refreshed_at = datetime.now(UTC)
        db_session.add(video)
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.get("/api/videos/vidformats1/formats")

        assert response.status_code == 200
        assert response.json() == {"heights": [1080, 720]}
        assert sent == []

    async def test_queues_a_probe_on_a_miss(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(_video("vidprobe123"))
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)
        monkeypatch.setattr("app.services.video_downloads.FORMATS_WAIT_SECONDS", 0)

        response = await client.get("/api/videos/vidprobe123/formats")

        assert response.status_code == 200
        assert response.json() == {"heights": []}
        assert sent == [
            (
                "app.tasks.formats.probe_video_formats",
                ["vidprobe123", "https://www.youtube.com/watch?v=vidprobe123"],
            )
        ]

    async def test_serves_a_stale_ladder_and_queues_a_refresh(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("vidstale123")
        video.available_heights = [720]
        video.formats_refreshed_at = datetime.now(UTC) - timedelta(days=2)
        db_session.add(video)
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.get("/api/videos/vidstale123/formats")

        assert response.status_code == 200
        assert response.json() == {"heights": [720]}
        assert sent == [
            (
                "app.tasks.formats.probe_video_formats",
                ["vidstale123", "https://www.youtube.com/watch?v=vidstale123"],
            )
        ]

    async def test_a_drive_video_answers_empty_without_probing(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        file_id = "1DOoJhC6NHpLSu2nAvaWy3HukVimEyyhE"
        video = Video(
            video_url=f"https://drive.google.com/file/d/{file_id}",
            video_id=file_id,
            title="Drive Video",
            language="en",
        )
        db_session.add(video)
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.get(f"/api/videos/{file_id}/formats")

        assert response.status_code == 200
        assert response.json() == {"heights": []}
        assert sent == []


class TestDownloadCreate:
    """POST /api/videos/{youtube_video_id}/downloads."""

    async def test_queues_a_download_for_the_worker(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(_video("vidqueue"))
        await db_session.flush()
        sent = _record_dispatches(monkeypatch)

        response = await client.post("/api/videos/vidqueue/downloads")

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        assert sent == [("app.tasks.downloads.download_video", [body["id"]])]

    async def test_records_the_requested_section_and_quality(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(_video("vidcreate"))
        await db_session.flush()
        _record_dispatches(monkeypatch)

        created = await client.post(
            "/api/videos/vidcreate/downloads",
            json={"start": 30, "end": 500, "height": 360},
        )
        download_id = created.json()["id"]

        response = await client.get(f"/api/videos/vidcreate/downloads/{download_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pending"
        assert body["url"] is None
        assert body["filename"] == "video-vidcreate-360p-30-100.mp4"

    async def test_unknown_video_returns_404(self, client: AsyncClient) -> None:
        response = await client.post("/api/videos/nosuchvideo/downloads")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    async def test_rejects_an_end_without_a_start(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        db_session.add(_video("viddomainbad"))
        await db_session.flush()

        response = await client.post("/api/videos/viddomainbad/downloads", json={"end": 90})

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["field"] == "end"

    @pytest.mark.parametrize(
        "payload",
        [{"height": 100}, {"height": 1440}, {"start": -5}],
    )
    async def test_rejects_requests_off_the_contract(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        payload: dict[str, int],
    ) -> None:
        db_session.add(_video("vidboundarybad"))
        await db_session.flush()

        response = await client.post("/api/videos/vidboundarybad/downloads", json=payload)

        assert response.status_code == 422


class TestDownloadStatus:
    """GET /api/videos/{youtube_video_id}/downloads/{download_id}."""

    async def test_unknown_download_returns_404(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        db_session.add(_video("vidstatus404"))
        await db_session.flush()

        response = await client.get(
            "/api/videos/vidstatus404/downloads/00000000-0000-0000-0000-000000000000"
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    async def test_a_download_does_not_resolve_under_another_video(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = _video("vidowner")
        other = _video("vidother")
        db_session.add_all([video, other])
        await db_session.flush()
        download = await _seed_download(db_session, video)

        response = await client.get(f"/api/videos/vidother/downloads/{download.id}")

        assert response.status_code == 404

    async def test_reports_the_blob_url_while_ready(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = _video("vidready")
        db_session.add(video)
        await db_session.flush()
        download = await _seed_download(
            db_session, video, status=STATUS_READY, blob_url="https://blob/file"
        )

        response = await client.get(f"/api/videos/vidready/downloads/{download.id}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["url"] == "https://blob/file"

    async def test_a_non_uuid_id_is_rejected(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        db_session.add(_video("viduuid"))
        await db_session.flush()

        response = await client.get("/api/videos/viduuid/downloads/not-a-uuid")

        assert response.status_code == 422


class TestDownloadConsume:
    """POST /api/videos/{youtube_video_id}/downloads/{download_id}/consume."""

    async def test_deletes_the_blob_and_marks_consumed(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("vidconsume")
        db_session.add(video)
        await db_session.flush()
        download = await _seed_download(
            db_session, video, status=STATUS_READY, blob_url="https://blob/file"
        )
        blob = BlobRecorder()
        monkeypatch.setattr("app.services.video_downloads.delete_blob_url", blob)

        response = await client.post(f"/api/videos/vidconsume/downloads/{download.id}/consume")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "consumed"
        assert body["url"] is None
        assert blob.urls == ["https://blob/file"]

    async def test_is_idempotent(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("vidtwice")
        db_session.add(video)
        await db_session.flush()
        download = await _seed_download(
            db_session, video, status=STATUS_READY, blob_url="https://blob/file"
        )
        blob = BlobRecorder()
        monkeypatch.setattr("app.services.video_downloads.delete_blob_url", blob)

        await client.post(f"/api/videos/vidtwice/downloads/{download.id}/consume")
        response = await client.post(f"/api/videos/vidtwice/downloads/{download.id}/consume")

        assert response.json()["status"] == "consumed"
        assert blob.urls == ["https://blob/file"]

    async def test_survives_a_blob_delete_failure(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("vidblobdown")
        db_session.add(video)
        await db_session.flush()
        download = await _seed_download(
            db_session, video, status=STATUS_READY, blob_url="https://blob/file"
        )
        monkeypatch.setattr("app.services.video_downloads.delete_blob_url", BlobRecorder(fail=True))

        response = await client.post(f"/api/videos/vidblobdown/downloads/{download.id}/consume")

        assert response.status_code == 200
        assert response.json()["status"] == "consumed"

    async def test_a_download_without_a_blob_is_still_consumed(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("vidnoblob")
        db_session.add(video)
        await db_session.flush()
        download = await _seed_download(db_session, video)
        blob = BlobRecorder()
        monkeypatch.setattr("app.services.video_downloads.delete_blob_url", blob)

        response = await client.post(f"/api/videos/vidnoblob/downloads/{download.id}/consume")

        assert response.json()["status"] == "consumed"
        assert blob.urls == []


class TestDownloadSweep:
    """The retention sweep that runs on every create."""

    async def test_create_retires_stale_and_expired_rows(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        video = _video("vidsweep")
        db_session.add(video)
        await db_session.flush()
        stale = await _seed_download(db_session, video)
        expired = await _seed_download(
            db_session, video, status=STATUS_CONSUMED, blob_url="https://blob/old"
        )
        stale.updated_at = datetime.now(UTC) - timedelta(minutes=31)
        expired.updated_at = datetime.now(UTC) - timedelta(days=2)
        await db_session.flush()
        blob = BlobRecorder()
        monkeypatch.setattr("app.services.video_downloads.delete_blob_url", blob)
        _record_dispatches(monkeypatch)

        response = await client.post("/api/videos/vidsweep/downloads")

        assert response.status_code == 202
        await db_session.refresh(stale)
        assert stale.status == "failed"
        assert stale.error == SWEEP_FAILURE_MESSAGE
        assert await db_session.get(VideoDownload, expired.id) is None
        assert blob.urls == ["https://blob/old"]
