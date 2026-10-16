"""Integration tests for the video MP4 download endpoint."""

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Video

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"fake-video-payload" * 100


def _video(video_id: str) -> Video:
    """Build a stored video row."""
    return Video(
        youtube_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
        title="Test Video",
        duration_seconds=100,
        language="en",
    )


def _served_file(tmp_path: Path, name: str = "video.mp4") -> Path:
    """Create the temp directory tree a finished download would return."""
    directory = tmp_path / "download"
    directory.mkdir(exist_ok=True)
    served = directory / name
    served.write_bytes(MP4_BYTES)
    return served


@pytest.mark.asyncio
async def test_unknown_video_returns_404(client: AsyncClient) -> None:
    response = await client.get("/api/videos/nosuchvideo/video.mp4")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_returns_mp4_attachment_for_stored_video(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_session.add(_video("vid123"))
    await db_session.flush()
    served = _served_file(tmp_path)
    directory = served.parent

    async def fake_download(
        youtube_video_id: str,
        *,
        start_seconds: int | None = None,
        end_seconds: int | None = None,
        height: int | None = None,
    ) -> Path:
        return served

    monkeypatch.setattr("app.api.routes.download_video_file", fake_download)

    response = await client.get("/api/videos/vid123/video.mp4")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-disposition"] == 'attachment; filename="video-vid123.mp4"'
    assert response.content == MP4_BYTES
    assert not directory.exists()


@pytest.mark.asyncio
async def test_returns_503_when_tooling_is_missing(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(_video("vid503"))
    await db_session.flush()
    monkeypatch.setattr("app.services.video_download.shutil.which", lambda name: None)

    response = await client.get("/api/videos/vid503/video.mp4")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_returns_502_when_the_download_fails(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(_video("vid502"))
    await db_session.flush()

    async def failing_run(command: list[str]) -> tuple[int, bytes]:
        return 1, b"ERROR: Video unavailable"

    monkeypatch.setattr("app.services.video_download._run_yt_dlp", failing_run)
    monkeypatch.setattr("app.services.video_download._RETRY_DELAY_SECONDS", 0)

    response = await client.get("/api/videos/vid502/video.mp4")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EXTERNAL_SERVICE_ERROR"


class TestDownloadRange:
    """The start/end query parameters that pick a section of the video."""

    @staticmethod
    def _record_download(monkeypatch: pytest.MonkeyPatch, served: Path) -> list[dict[str, object]]:
        calls: list[dict[str, object]] = []

        async def fake_download(youtube_video_id: str, **kwargs: object) -> Path:
            calls.append({"youtube_video_id": youtube_video_id, **kwargs})
            return served

        monkeypatch.setattr("app.api.routes.download_video_file", fake_download)
        return calls

    async def test_returns_the_requested_section_with_a_range_filename(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        db_session.add(_video("vidrange"))
        await db_session.flush()
        calls = self._record_download(monkeypatch, _served_file(tmp_path))

        response = await client.get("/api/videos/vidrange/video.mp4?start=30&end=90")

        assert response.status_code == 200
        assert (
            response.headers["content-disposition"]
            == 'attachment; filename="video-vidrange-30-90.mp4"'
        )
        assert calls == [
            {
                "youtube_video_id": "vidrange",
                "start_seconds": 30,
                "end_seconds": 90,
                "height": None,
            }
        ]

    async def test_leaves_the_end_open_when_only_start_is_given(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        db_session.add(_video("vidopen"))
        await db_session.flush()
        calls = self._record_download(monkeypatch, _served_file(tmp_path))

        response = await client.get("/api/videos/vidopen/video.mp4?start=30")

        assert response.status_code == 200
        assert (
            response.headers["content-disposition"]
            == 'attachment; filename="video-vidopen-30-end.mp4"'
        )
        assert calls == [
            {
                "youtube_video_id": "vidopen",
                "start_seconds": 30,
                "end_seconds": None,
                "height": None,
            }
        ]

    async def test_truncates_fractional_seconds_to_whole_ones(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        db_session.add(_video("vidfraction"))
        await db_session.flush()
        calls = self._record_download(monkeypatch, _served_file(tmp_path))

        response = await client.get("/api/videos/vidfraction/video.mp4?start=17.533")

        assert response.status_code == 200
        assert (
            response.headers["content-disposition"]
            == 'attachment; filename="video-vidfraction-17-end.mp4"'
        )
        assert calls == [
            {
                "youtube_video_id": "vidfraction",
                "start_seconds": 17,
                "end_seconds": None,
                "height": None,
            }
        ]

    async def test_clamps_an_end_beyond_the_video_duration(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        db_session.add(_video("vidclamp"))
        await db_session.flush()
        calls = self._record_download(monkeypatch, _served_file(tmp_path))

        response = await client.get("/api/videos/vidclamp/video.mp4?start=30&end=500")

        assert response.status_code == 200
        assert (
            response.headers["content-disposition"]
            == 'attachment; filename="video-vidclamp-30-100.mp4"'
        )
        assert calls == [
            {
                "youtube_video_id": "vidclamp",
                "start_seconds": 30,
                "end_seconds": 100,
                "height": None,
            }
        ]

    @pytest.mark.parametrize(
        ("query", "field"),
        [
            ("?end=90", "end"),
            ("?start=90&end=30", "end"),
            ("?start=17.5&end=17.9", "end"),
            ("?start=100", "start"),
            ("?start=500", "start"),
        ],
    )
    async def test_rejects_an_impossible_range_before_downloading(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        query: str,
        field: str,
    ) -> None:
        db_session.add(_video("vidbad"))
        await db_session.flush()
        calls = self._record_download(monkeypatch, _served_file(tmp_path))

        response = await client.get(f"/api/videos/vidbad/video.mp4{query}")

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["field"] == field
        assert calls == []

    async def test_rejects_a_negative_start_at_the_boundary(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        db_session.add(_video("vidnegative"))
        await db_session.flush()

        response = await client.get("/api/videos/vidnegative/video.mp4?start=-5")

        assert response.status_code == 422


class TestDownloadQuality:
    """The height query parameter that caps the download quality."""

    @staticmethod
    def _record_download(monkeypatch: pytest.MonkeyPatch, served: Path) -> list[dict[str, object]]:
        calls: list[dict[str, object]] = []

        async def fake_download(youtube_video_id: str, **kwargs: object) -> Path:
            calls.append({"youtube_video_id": youtube_video_id, **kwargs})
            return served

        monkeypatch.setattr("app.api.routes.download_video_file", fake_download)
        return calls

    async def test_returns_the_requested_quality_with_a_quality_filename(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        db_session.add(_video("vidquality"))
        await db_session.flush()
        calls = self._record_download(monkeypatch, _served_file(tmp_path))

        response = await client.get("/api/videos/vidquality/video.mp4?height=720")

        assert response.status_code == 200
        assert (
            response.headers["content-disposition"]
            == 'attachment; filename="video-vidquality-720p.mp4"'
        )
        assert calls == [
            {
                "youtube_video_id": "vidquality",
                "start_seconds": None,
                "end_seconds": None,
                "height": 720,
            }
        ]

    async def test_orders_quality_before_the_range_in_the_filename(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        db_session.add(_video("vidboth"))
        await db_session.flush()
        calls = self._record_download(monkeypatch, _served_file(tmp_path))

        response = await client.get("/api/videos/vidboth/video.mp4?start=30&end=90&height=360")

        assert response.status_code == 200
        assert (
            response.headers["content-disposition"]
            == 'attachment; filename="video-vidboth-360p-30-90.mp4"'
        )
        assert calls == [
            {"youtube_video_id": "vidboth", "start_seconds": 30, "end_seconds": 90, "height": 360}
        ]

    @pytest.mark.parametrize("query", ["?height=100", "?height=1440"])
    async def test_rejects_a_height_off_the_ladder_before_downloading(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        query: str,
    ) -> None:
        db_session.add(_video("vidbadquality"))
        await db_session.flush()
        calls = self._record_download(monkeypatch, _served_file(tmp_path))

        response = await client.get(f"/api/videos/vidbadquality/video.mp4{query}")

        assert response.status_code == 422
        assert calls == []


class TestVideoFormats:
    """The formats endpoint that lists the downloadable heights."""

    @staticmethod
    def _payload(heights: list[int]) -> bytes:
        formats = [{"vcodec": "avc1.640032", "height": h} for h in heights]
        return json.dumps({"formats": formats}).encode()

    async def test_unknown_video_returns_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/videos/nosuchvideo/formats")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    async def test_lists_the_probed_heights_tallest_first(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(_video("vidformats"))
        await db_session.flush()

        async def fake_probe(command: list[str]) -> tuple[int, bytes, bytes]:
            return 0, self._payload([1080, 720]), b""

        monkeypatch.setattr("app.services.video_download._run_probe", fake_probe)

        response = await client.get("/api/videos/vidformats/formats")

        assert response.status_code == 200
        assert response.json() == {"heights": [1080, 720]}

    async def test_returns_502_when_the_probe_fails(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(_video("vidprobe502"))
        await db_session.flush()

        async def failing_probe(command: list[str]) -> tuple[int, bytes, bytes]:
            return 1, b"", b"ERROR: Video unavailable"

        monkeypatch.setattr("app.services.video_download._run_probe", failing_probe)

        response = await client.get("/api/videos/vidprobe502/formats")

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "EXTERNAL_SERVICE_ERROR"

    async def test_returns_503_when_tooling_is_missing(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(_video("vidprobe503"))
        await db_session.flush()
        monkeypatch.setattr("app.services.video_download.shutil.which", lambda name: None)

        response = await client.get("/api/videos/vidprobe503/formats")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
