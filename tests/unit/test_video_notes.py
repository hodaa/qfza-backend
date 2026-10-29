"""Unit tests for the lecture-notes lifecycle service."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import NotesNotFoundError, VideoNotFoundError, VideoNotTranscribedError
from app.models import Video, VideoNote
from app.services.video_notes import (
    NOTES_STALE_ERROR,
    NOTES_STALE_SECONDS,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_READY,
    VideoNotesService,
)

_VIDEO_ID = "dQw4w9WgXcQ"


def _video(*, transcribed: bool = True) -> Video:
    """Build a transient video row with a stable id."""
    video = Video(
        video_url=f"https://www.youtube.com/watch?v={_VIDEO_ID}",
        video_id=_VIDEO_ID,
        title="Linear Algebra",
        duration_seconds=1200,
        language="en",
        transcribed_at=datetime.now(UTC) if transcribed else None,
    )
    video.id = uuid4()
    return video


def _note(
    video_id: UUID,
    *,
    status: str = STATUS_PENDING,
    blob_url: str | None = None,
    age_seconds: int = 0,
) -> VideoNote:
    """Build a transient lecture-notes row with a stable id."""
    note = VideoNote(video_id=video_id, status=status, blob_url=blob_url)
    note.id = uuid4()
    note.updated_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return note


class FakeSession:
    """Record rollbacks made while resolving a create race."""

    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeVideoRepo:
    """Video repository stand-in backed by transient rows."""

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


class FakeNoteRepo:
    """Notes repository stand-in with knobs for caching and races."""

    def __init__(self, *, active_sequence: list[VideoNote | None] | None = None) -> None:
        self.rows: dict[UUID, VideoNote] = {}
        self.ready: VideoNote | None = None
        self.active_sequence = list(active_sequence or [])
        self.active: VideoNote | None = None
        self.create_raises = False
        self.session = FakeSession()

    async def create(self, *, video_id: UUID) -> VideoNote:
        if self.create_raises:
            raise IntegrityError("insert", {}, Exception("duplicate"))
        row = _note(video_id)
        self.rows[row.id] = row
        return row

    async def get_by_id(self, note_id: UUID) -> VideoNote | None:
        return self.rows.get(note_id)

    async def get_for_video(self, note_id: UUID, video_id: UUID) -> VideoNote | None:
        row = self.rows.get(note_id)
        if row is None or row.video_id != video_id:
            return None
        return row

    async def get_active(self, video_id: UUID) -> VideoNote | None:
        if self.active_sequence:
            return self.active_sequence.pop(0)
        return self.active

    async def get_ready(self, video_id: UUID) -> VideoNote | None:
        return self.ready

    async def update_status(
        self,
        note_id: UUID,
        *,
        status: str,
        blob_url: str | None = None,
        error: str | None = None,
    ) -> VideoNote:
        row = self.rows[note_id]
        row.status = status
        if blob_url is not None:
            row.blob_url = blob_url
        if error is not None:
            row.error = error
        return row


def _service(
    video: Video | None,
    repo: FakeNoteRepo | None = None,
) -> tuple[VideoNotesService, FakeNoteRepo]:
    """Build the service under test with recording fakes attached."""
    note_repo = repo or FakeNoteRepo()
    return VideoNotesService(FakeVideoRepo(video), note_repo), note_repo


class TestCreate:
    """Queueing and caching lecture-notes rows."""

    @pytest.mark.asyncio
    async def test_unknown_video_raises_not_found(self) -> None:
        service, _ = _service(None)

        with pytest.raises(VideoNotFoundError):
            await service.create("nosuchvideo")

    @pytest.mark.asyncio
    async def test_untranscribed_video_raises(self) -> None:
        service, _ = _service(_video(transcribed=False))

        with pytest.raises(VideoNotTranscribedError):
            await service.create(_VIDEO_ID)

    @pytest.mark.asyncio
    async def test_returns_a_cached_ready_pdf(self) -> None:
        video = _video()
        repo = FakeNoteRepo()
        repo.ready = _note(video.id, status=STATUS_READY, blob_url="https://blob/notes.pdf")
        service, _ = _service(video, repo)

        note = await service.create(_VIDEO_ID)

        assert note is repo.ready

    @pytest.mark.asyncio
    async def test_returns_a_fresh_active_request(self) -> None:
        video = _video()
        repo = FakeNoteRepo()
        repo.active = _note(video.id, status=STATUS_PROCESSING)
        service, _ = _service(video, repo)

        note = await service.create(_VIDEO_ID)

        assert note is repo.active

    @pytest.mark.asyncio
    async def test_expires_a_stale_request_and_creates_a_new_one(self) -> None:
        video = _video()
        repo = FakeNoteRepo()
        stale = _note(video.id, status=STATUS_PROCESSING, age_seconds=NOTES_STALE_SECONDS + 1)
        repo.rows[stale.id] = stale
        repo.active_sequence = [stale]
        service, _ = _service(video, repo)

        note = await service.create(_VIDEO_ID)

        assert stale.status == STATUS_FAILED
        assert stale.error == NOTES_STALE_ERROR
        assert note.status == STATUS_PENDING
        assert note.id != stale.id

    @pytest.mark.asyncio
    async def test_resolves_a_concurrent_create_race(self) -> None:
        video = _video()
        repo = FakeNoteRepo()
        winner = _note(video.id)
        repo.rows[winner.id] = winner
        repo.active_sequence = [None, winner]
        repo.create_raises = True
        service, _ = _service(video, repo)

        note = await service.create(_VIDEO_ID)

        assert note is winner
        assert repo.session.rollbacks == 1

    @pytest.mark.asyncio
    async def test_reraises_a_race_without_a_winner(self) -> None:
        video = _video()
        repo = FakeNoteRepo()
        repo.active_sequence = [None, None]
        repo.create_raises = True
        service, _ = _service(video, repo)

        with pytest.raises(IntegrityError):
            await service.create(_VIDEO_ID)


class TestLookups:
    """Fetch and scoping behavior for notes."""

    @pytest.mark.asyncio
    async def test_get_is_scoped_to_its_video(self) -> None:
        owner = _video()
        other = Video(
            video_url="https://www.youtube.com/watch?v=vidother123",
            video_id="vidother123",
            title="Other",
            transcribed_at=datetime.now(UTC),
        )
        other.id = uuid4()
        repo = FakeNoteRepo()
        row = _note(owner.id)
        repo.rows[row.id] = row
        service = VideoNotesService(FakeVideoRepo([owner, other]), repo)

        with pytest.raises(NotesNotFoundError):
            await service.get("vidother123", row.id)

    @pytest.mark.asyncio
    async def test_get_by_id_raises_for_an_unknown_row(self) -> None:
        service, _ = _service(_video())

        with pytest.raises(NotesNotFoundError):
            await service.get_by_id(uuid4())

    @pytest.mark.asyncio
    async def test_get_for_worker_returns_the_note_and_video(self) -> None:
        video = _video()
        repo = FakeNoteRepo()
        row = _note(video.id)
        repo.rows[row.id] = row
        service, _ = _service(video, repo)

        note, fetched_video = await service.get_for_worker(row.id)

        assert note is row
        assert fetched_video is video

    @pytest.mark.asyncio
    async def test_get_for_worker_raises_when_the_video_is_gone(self) -> None:
        repo = FakeNoteRepo()
        row = _note(uuid4())
        repo.rows[row.id] = row
        service, _ = _service(None, repo)

        with pytest.raises(VideoNotFoundError):
            await service.get_for_worker(row.id)


class TestWorkerReporting:
    """The lifecycle callbacks the worker makes."""

    @pytest.mark.asyncio
    async def test_advances_to_processing(self) -> None:
        video = _video()
        repo = FakeNoteRepo()
        row = _note(video.id)
        repo.rows[row.id] = row
        service, _ = _service(video, repo)

        result = await service.mark_processing(row.id)

        assert result.status == STATUS_PROCESSING

    @pytest.mark.asyncio
    async def test_publishes_the_pdf_url(self) -> None:
        video = _video()
        repo = FakeNoteRepo()
        row = _note(video.id)
        repo.rows[row.id] = row
        service, _ = _service(video, repo)

        result = await service.mark_ready(row.id, "https://blob/notes.pdf")

        assert result.status == STATUS_READY
        assert result.blob_url == "https://blob/notes.pdf"

    @pytest.mark.asyncio
    async def test_records_a_failure_message(self) -> None:
        video = _video()
        repo = FakeNoteRepo()
        row = _note(video.id)
        repo.rows[row.id] = row
        service, _ = _service(video, repo)

        result = await service.mark_failed(row.id, "generation failed")

        assert result.status == STATUS_FAILED
        assert result.error == "generation failed"

    @pytest.mark.asyncio
    async def test_callbacks_raise_for_an_unknown_row(self) -> None:
        service, _ = _service(_video())

        with pytest.raises(NotesNotFoundError):
            await service.mark_processing(uuid4())
