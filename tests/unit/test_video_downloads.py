"""Unit tests for the offloaded download ledger and formats cache services."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.exceptions import DownloadNotFoundError, ValidationError, VideoNotFoundError
from app.models import Video, VideoDownload
from app.services.video_downloads import (
    STATUS_CONSUMED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_READY,
    SWEEP_FAILURE_MESSAGE,
    VideoDownloadService,
    VideoFormatsService,
    download_filename,
    validated_range,
)

#: A real-shaped YouTube id: the source strategies only claim 11-char ids.
_VIDEO_ID = "dQw4w9WgXcQ"


def _video(video_id: str = _VIDEO_ID, duration: int | None = 100) -> Video:
    """Build a transient video row with a stable id."""
    video = Video(
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
        title="Test Video",
        duration_seconds=duration,
        language="en",
    )
    video.id = uuid4()
    return video


def _download(
    video_id: UUID,
    *,
    status: str = STATUS_PENDING,
    blob_url: str | None = None,
    start: int | None = None,
    end: int | None = None,
    height: int | None = None,
) -> VideoDownload:
    """Build a transient download row with a stable id."""
    download = VideoDownload(
        video_id=video_id,
        status=status,
        blob_url=blob_url,
        start_seconds=start,
        end_seconds=end,
        height=height,
    )
    download.id = uuid4()
    return download


class FakeVideoRepo:
    """Video repository stand-in backed by transient model objects."""

    def __init__(self, videos: Video | list[Video] | None) -> None:
        if videos is None:
            self.videos: list[Video] = []
        elif isinstance(videos, Video):
            self.videos = [videos]
        else:
            self.videos = list(videos)

    async def get_by_video_id_lite(self, youtube_video_id: str) -> Video | None:
        for video in self.videos:
            if video.video_id == youtube_video_id:
                return video
        return None

    async def get_by_id_lite(self, video_id: UUID) -> Video | None:
        for video in self.videos:
            if video.id == video_id:
                return video
        return None


class FakeDownloadRepo:
    """Download repository stand-in with sweep knobs for each test."""

    def __init__(self) -> None:
        self.rows: dict[UUID, VideoDownload] = {}
        self.stale: list[VideoDownload] = []
        self.expired: list[VideoDownload] = []
        self.deleted_ids: list[UUID] = []
        self.fail_sweep = False

    async def create(
        self,
        *,
        video_id: UUID,
        start_seconds: int | None,
        end_seconds: int | None,
        height: int | None,
    ) -> VideoDownload:
        row = _download(video_id, start=start_seconds, end=end_seconds, height=height)
        self.rows[row.id] = row
        return row

    async def get_by_id(self, download_id: UUID) -> VideoDownload | None:
        return self.rows.get(download_id)

    async def get_for_video(self, download_id: UUID, video_id: UUID) -> VideoDownload | None:
        row = self.rows.get(download_id)
        if row is None or row.video_id != video_id:
            return None
        return row

    async def update_status(
        self,
        download_id: UUID,
        *,
        status: str,
        blob_url: str | None = None,
        error: str | None = None,
        consumed: bool = False,
    ) -> VideoDownload:
        row = self.rows[download_id]
        row.status = status
        if blob_url is not None:
            row.blob_url = blob_url
        if error is not None:
            row.error = error
        if consumed:
            row.consumed_at = datetime.now(UTC)
        return row

    async def list_stale_active(self, cutoff: datetime, limit: int) -> list[VideoDownload]:
        if self.fail_sweep:
            raise RuntimeError("sweep query exploded")
        return self.stale

    async def list_expired(self, cutoff: datetime, limit: int) -> list[VideoDownload]:
        return self.expired

    async def delete_ids(self, download_ids: list[UUID]) -> None:
        self.deleted_ids.extend(download_ids)
        for download_id in download_ids:
            self.rows.pop(download_id, None)


class BlobRecorder:
    """Async blob deleter that records calls and can be made to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.urls: list[str] = []
        self.fail = fail

    async def __call__(self, blob_url: str) -> None:
        self.urls.append(blob_url)
        if self.fail:
            raise RuntimeError("blob store is down")


class FakeSession:
    """Session stand-in whose refresh optionally lets a fake probe land."""

    def __init__(self, on_refresh: Callable[[Video], None] | None = None) -> None:
        self._on_refresh = on_refresh
        self.refreshes = 0

    async def refresh(self, video: Video) -> None:
        self.refreshes += 1
        if self._on_refresh is not None:
            self._on_refresh(video)


def _service(
    video: Video | None,
    *,
    deleter: BlobRecorder | None = None,
    repo: FakeDownloadRepo | None = None,
) -> tuple[VideoDownloadService, FakeDownloadRepo, BlobRecorder]:
    """Build the service under test with recording fakes attached."""
    download_repo = repo or FakeDownloadRepo()
    blob = deleter or BlobRecorder()
    service = VideoDownloadService(FakeVideoRepo(video), download_repo, blob_deleter=blob)
    return service, download_repo, blob


def _formats_service(
    video: Video | None,
    *,
    dispatch_calls: list[tuple[str, str]] | None = None,
    fail_dispatch: bool = False,
    session: FakeSession | None = None,
) -> VideoFormatsService:
    """Build the formats service with an injected dispatch and fake session."""
    calls = dispatch_calls if dispatch_calls is not None else []

    def dispatch(youtube_video_id: str, youtube_url: str) -> None:
        calls.append((youtube_video_id, youtube_url))
        if fail_dispatch:
            raise RuntimeError("broker is down")

    async def no_sleep(seconds: float) -> None:
        return None

    return VideoFormatsService(
        FakeVideoRepo(video),
        session or FakeSession(),
        dispatch=dispatch,
        sleep=no_sleep,
    )


class TestDownloadFilename:
    """The filename builder shared by the download status and consume routes."""

    def test_builds_a_plain_name(self) -> None:
        assert download_filename("dQw4w9WgXcQ") == "video-dQw4w9WgXcQ.mp4"

    def test_strips_characters_unsafe_for_a_header(self) -> None:
        assert download_filename("a/b c?d") == "video-abcd.mp4"

    def test_falls_back_when_nothing_safe_remains(self) -> None:
        assert download_filename("///") == "video-video.mp4"

    def test_labels_the_height_before_the_range(self) -> None:
        name = download_filename("vid1", range_start=30, range_end=90, height=360)
        assert name == "video-vid1-360p-30-90.mp4"

    def test_marks_an_open_ended_range(self) -> None:
        assert download_filename("vid1", range_start=30) == "video-vid1-30-end.mp4"

    def test_truncates_an_absurdly_long_id(self) -> None:
        name = download_filename("x" * 100)
        assert name == f"video-{'x' * 64}.mp4"


class TestValidatedRange:
    """The start/end pair checked against the video's duration."""

    def test_returns_nothing_when_no_range_is_asked(self) -> None:
        assert validated_range(None, None, 100) == (None, None)

    def test_truncates_and_clamps_a_requested_range(self) -> None:
        assert validated_range(17.533, 500.0, 100) == (17, 100)

    def test_keeps_an_end_when_the_duration_is_unknown(self) -> None:
        assert validated_range(30.0, 90.0, None) == (30, 90)

    def test_rejects_an_end_without_a_start(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validated_range(None, 90.0, 100)
        assert exc.value.details["field"] == "end"

    @pytest.mark.parametrize(("start", "end"), [(90.0, 30.0), (17.5, 17.9)])
    def test_rejects_an_end_not_after_the_start(self, start: float, end: float) -> None:
        with pytest.raises(ValidationError) as exc:
            validated_range(start, end, 100)
        assert exc.value.details["field"] == "end"

    def test_rejects_a_start_beyond_the_duration(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validated_range(100.0, None, 100)
        assert exc.value.details["field"] == "start"


class TestCreate:
    """Queueing a download row."""

    @pytest.mark.asyncio
    async def test_stores_a_pending_row_with_the_validated_range(self) -> None:
        video = _video()
        service, repo, _ = _service(video)

        row = await service.create(_VIDEO_ID, start=30.0, end=500.0, height=360)

        assert row.status == STATUS_PENDING
        assert row.video_id == video.id
        assert (row.start_seconds, row.end_seconds, row.height) == (30, 100, 360)
        assert repo.rows[row.id] is row

    @pytest.mark.asyncio
    async def test_unknown_video_raises_not_found(self) -> None:
        service, _, _ = _service(None)

        with pytest.raises(VideoNotFoundError):
            await service.create("nosuchvideo")

    @pytest.mark.asyncio
    async def test_runs_the_sweep_before_queueing(self) -> None:
        video = _video()
        service, repo, blob = _service(video)
        stale = _download(video.id)
        expired = _download(video.id, status=STATUS_CONSUMED, blob_url="https://blob/old")
        repo.rows[stale.id] = stale
        repo.rows[expired.id] = expired
        repo.stale = [stale]
        repo.expired = [expired]

        await service.create(_VIDEO_ID)

        assert stale.status == STATUS_FAILED
        assert stale.error == SWEEP_FAILURE_MESSAGE
        assert blob.urls == ["https://blob/old"]
        assert repo.deleted_ids == [expired.id]

    @pytest.mark.asyncio
    async def test_a_broken_sweep_never_fails_the_request(self) -> None:
        video = _video()
        service, repo, _ = _service(video)
        repo.fail_sweep = True

        row = await service.create(_VIDEO_ID)

        assert row.status == STATUS_PENDING


class TestConsume:
    """Deleting the blob once the client has the file."""

    @pytest.mark.asyncio
    async def test_deletes_the_blob_and_marks_the_row_consumed(self) -> None:
        video = _video()
        service, repo, blob = _service(video)
        row = _download(video.id, status=STATUS_READY, blob_url="https://blob/file")
        repo.rows[row.id] = row

        result = await service.consume(_VIDEO_ID, row.id)

        assert result.status == STATUS_CONSUMED
        assert result.consumed_at is not None
        assert blob.urls == ["https://blob/file"]

    @pytest.mark.asyncio
    async def test_is_idempotent(self) -> None:
        video = _video()
        service, repo, blob = _service(video)
        row = _download(video.id, status=STATUS_READY, blob_url="https://blob/file")
        repo.rows[row.id] = row

        await service.consume(_VIDEO_ID, row.id)
        result = await service.consume(_VIDEO_ID, row.id)

        assert result.status == STATUS_CONSUMED
        assert blob.urls == ["https://blob/file"]

    @pytest.mark.asyncio
    async def test_a_row_without_a_blob_skips_deletion(self) -> None:
        video = _video()
        service, repo, blob = _service(video)
        row = _download(video.id)
        repo.rows[row.id] = row

        result = await service.consume(_VIDEO_ID, row.id)

        assert result.status == STATUS_CONSUMED
        assert blob.urls == []

    @pytest.mark.asyncio
    async def test_a_failing_blob_delete_still_consumes(self) -> None:
        video = _video()
        service, repo, blob = _service(video, deleter=BlobRecorder(fail=True))
        row = _download(video.id, status=STATUS_READY, blob_url="https://blob/file")
        repo.rows[row.id] = row

        result = await service.consume(_VIDEO_ID, row.id)

        assert result.status == STATUS_CONSUMED
        assert blob.urls == ["https://blob/file"]


class TestWorkerReporting:
    """The lifecycle calls the worker makes through the internal API."""

    @pytest.mark.asyncio
    async def test_advances_to_processing(self) -> None:
        video = _video()
        service, repo, _ = _service(video)
        row = _download(video.id)
        repo.rows[row.id] = row

        result = await service.mark_processing(row.id)

        assert result.status == STATUS_PROCESSING

    @pytest.mark.asyncio
    async def test_publishes_the_blob_url(self) -> None:
        video = _video()
        service, repo, _ = _service(video)
        row = _download(video.id)
        repo.rows[row.id] = row

        result = await service.mark_ready(row.id, "https://blob/new")

        assert result.status == STATUS_READY
        assert result.blob_url == "https://blob/new"

    @pytest.mark.asyncio
    async def test_a_late_upload_for_a_consumed_row_is_deleted(self) -> None:
        video = _video()
        service, repo, blob = _service(video)
        row = _download(video.id, status=STATUS_CONSUMED, blob_url="https://blob/old")
        repo.rows[row.id] = row

        result = await service.mark_ready(row.id, "https://blob/late")

        assert result.status == STATUS_CONSUMED
        assert result.blob_url == "https://blob/old"
        assert blob.urls == ["https://blob/late"]

    @pytest.mark.asyncio
    async def test_records_a_failure_message(self) -> None:
        video = _video()
        service, repo, _ = _service(video)
        row = _download(video.id)
        repo.rows[row.id] = row

        result = await service.mark_failed(row.id, "The video is unavailable")

        assert result.status == STATUS_FAILED
        assert result.error == "The video is unavailable"


class TestLookups:
    """Fetch and scoping behavior for downloads."""

    @pytest.mark.asyncio
    async def test_get_is_scoped_to_its_video(self) -> None:
        owner = _video("vidowner")
        other = _video("vidother")
        repo = FakeDownloadRepo()
        row = _download(owner.id)
        repo.rows[row.id] = row
        service = VideoDownloadService(
            FakeVideoRepo([owner, other]), repo, blob_deleter=BlobRecorder()
        )

        with pytest.raises(DownloadNotFoundError):
            await service.get("vidother", row.id)

    @pytest.mark.asyncio
    async def test_get_by_id_raises_for_an_unknown_row(self) -> None:
        service, _, _ = _service(_video())

        with pytest.raises(DownloadNotFoundError):
            await service.get_by_id(uuid4())

    @pytest.mark.asyncio
    async def test_get_for_worker_returns_the_download_and_video(self) -> None:
        video = _video()
        service, repo, _ = _service(video)
        row = _download(video.id)
        repo.rows[row.id] = row

        download, fetched_video = await service.get_for_worker(row.id)

        assert download is row
        assert fetched_video is video

    @pytest.mark.asyncio
    async def test_get_for_worker_raises_when_the_video_is_gone(self) -> None:
        service, repo, _ = _service(None)
        row = _download(uuid4())
        repo.rows[row.id] = row

        with pytest.raises(VideoNotFoundError):
            await service.get_for_worker(row.id)


class TestFormatsCache:
    """Serving the quality ladder from the cached column."""

    @pytest.mark.asyncio
    async def test_a_fresh_ladder_is_served_without_a_probe(self) -> None:
        video = _video()
        video.available_heights = [1080, 720]
        video.formats_refreshed_at = datetime.now(UTC)
        calls: list[tuple[str, str]] = []

        heights = await _formats_service(video, dispatch_calls=calls).get_heights(_VIDEO_ID)

        assert heights == [1080, 720]
        assert calls == []

    @pytest.mark.asyncio
    async def test_an_empty_ladder_is_a_real_answer(self) -> None:
        video = _video()
        video.available_heights = []
        video.formats_refreshed_at = datetime.now(UTC)
        calls: list[tuple[str, str]] = []

        heights = await _formats_service(video, dispatch_calls=calls).get_heights(_VIDEO_ID)

        assert heights == []
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_stale_ladder_is_served_and_a_refresh_queued(self) -> None:
        video = _video()
        video.available_heights = [720]
        video.formats_refreshed_at = datetime.now(UTC) - timedelta(days=2)
        calls: list[tuple[str, str]] = []

        heights = await _formats_service(video, dispatch_calls=calls).get_heights(_VIDEO_ID)

        assert heights == [720]
        assert calls == [(_VIDEO_ID, f"https://www.youtube.com/watch?v={_VIDEO_ID}")]

    @pytest.mark.asyncio
    async def test_a_failing_refresh_dispatch_still_serves_the_ladder(self) -> None:
        video = _video()
        video.available_heights = [720]
        video.formats_refreshed_at = datetime.now(UTC) - timedelta(days=2)

        heights = await _formats_service(video, fail_dispatch=True).get_heights(_VIDEO_ID)

        assert heights == [720]

    @pytest.mark.asyncio
    async def test_unknown_video_raises_not_found(self) -> None:
        with pytest.raises(VideoNotFoundError):
            await _formats_service(None).get_heights("nosuchvideo")

    @pytest.mark.asyncio
    async def test_a_miss_waits_for_the_probe_to_land(self) -> None:
        video = _video()
        calls: list[tuple[str, str]] = []

        def on_refresh(row: Video) -> None:
            row.available_heights = [720]
            row.formats_refreshed_at = datetime.now(UTC)

        session = FakeSession(on_refresh)
        service = _formats_service(video, dispatch_calls=calls, session=session)

        heights = await service.get_heights(_VIDEO_ID)

        assert heights == [720]
        assert calls == [(_VIDEO_ID, f"https://www.youtube.com/watch?v={_VIDEO_ID}")]
        assert session.refreshes == 1

    @pytest.mark.asyncio
    async def test_a_probe_that_stores_nothing_answers_empty(self) -> None:
        video = _video()

        def on_refresh(row: Video) -> None:
            row.available_heights = []
            row.formats_refreshed_at = datetime.now(UTC)

        service = _formats_service(video, session=FakeSession(on_refresh))

        assert await service.get_heights(_VIDEO_ID) == []

    @pytest.mark.asyncio
    async def test_a_miss_times_out_to_an_empty_ladder(self) -> None:
        video = _video()
        session = FakeSession()

        heights = await _formats_service(video, session=session).get_heights(_VIDEO_ID)

        assert heights == []
        assert session.refreshes == 15

    @pytest.mark.asyncio
    async def test_a_failing_dispatch_on_a_miss_answers_empty(self) -> None:
        video = _video()
        session = FakeSession()

        service = _formats_service(video, fail_dispatch=True, session=session)

        assert await service.get_heights(_VIDEO_ID) == []
        assert session.refreshes == 0

    @pytest.mark.asyncio
    async def test_a_source_without_qualities_answers_empty_without_probing(self) -> None:
        video = _video()
        video.video_url = "https://drive.google.com/file/d/1DOoJhC6NHpLSu2nAvaWy3HukVimEyyhE"
        calls: list[tuple[str, str]] = []

        heights = await _formats_service(video, dispatch_calls=calls).get_heights(_VIDEO_ID)

        assert heights == []
        assert calls == []

    @pytest.mark.asyncio
    async def test_an_unclaimed_stored_url_answers_empty_without_probing(self) -> None:
        video = _video()
        video.video_url = "https://example.com/video"
        calls: list[tuple[str, str]] = []

        heights = await _formats_service(video, dispatch_calls=calls).get_heights(_VIDEO_ID)

        assert heights == []
        assert calls == []
