"""AI-generated lecture notes: the request ledger.

The backend never runs OCR or renders a PDF. It queues a ``video_notes`` row,
the worker reads the lecture's slides and transcript, uploads the finished PDF
to blob storage, and reports the blob URL back. A ready row is reused so a
second click does not pay for the same generation twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import NotesNotFoundError, VideoNotFoundError, VideoNotTranscribedError
from app.core.logging import get_logger
from app.models import Video, VideoNote
from app.repositories import VideoNoteRepository, VideoRepository

logger = get_logger(__name__)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

#: A worker killed mid-generation leaves a row active; past this age it is
#: failed so the next click can start a fresh request.
NOTES_STALE_SECONDS = 1800

#: User-safe failure recorded for a request the sweep gave up on.
NOTES_STALE_ERROR = "Lecture-notes generation timed out. Please try again."

#: Recorded on a cached PDF when the user asks for a fresh generation.
NOTES_SUPERSEDED_ERROR = "Superseded by a newer lecture-notes request."


async def _require_video(video_repo: VideoRepository, youtube_video_id: str) -> Video:
    """Resolve a video by its source id, or raise 404."""
    video = await video_repo.get_by_video_id_lite(youtube_video_id)
    if video is None:
        raise VideoNotFoundError(youtube_video_id)
    return video


class VideoNotesService:
    """Owns lecture-notes rows from queueing to the cached blob URL."""

    def __init__(self, video_repo: VideoRepository, note_repo: VideoNoteRepository) -> None:
        """Wire the service to its repositories."""
        self._video_repo = video_repo
        self._note_repo = note_repo

    async def create(self, youtube_video_id: str, *, regenerate: bool = False) -> VideoNote:
        """Return a cached/active row, or queue a new lecture-notes request."""
        video = await _require_video(self._video_repo, youtube_video_id)
        if not video.transcribed_at:
            raise VideoNotTranscribedError(youtube_video_id)

        active = await self._note_repo.get_active(video.id)
        if active is not None and not await self._expire_if_stale(active):
            return active
        if regenerate:
            await self._fail_ready(video.id)
            return await self._create_or_reuse(video.id)

        ready = await self._note_repo.get_ready(video.id)
        if ready is not None:
            return ready
        return await self._create_or_reuse(video.id)

    async def get(self, youtube_video_id: str, note_id: UUID) -> VideoNote:
        """Return one note scoped to its video, or 404."""
        video = await _require_video(self._video_repo, youtube_video_id)
        note = await self._note_repo.get_for_video(note_id, video.id)
        if note is None:
            raise NotesNotFoundError(str(note_id))
        return note

    async def get_by_id(self, note_id: UUID) -> VideoNote:
        """Return a note by id, or 404."""
        note = await self._note_repo.get_by_id(note_id)
        if note is None:
            raise NotesNotFoundError(str(note_id))
        return note

    async def get_for_worker(self, note_id: UUID) -> tuple[VideoNote, Video]:
        """Return a note and its video for the worker's fetch."""
        note = await self.get_by_id(note_id)
        video = await self._video_repo.get_by_id_lite(note.video_id)
        if video is None:
            raise VideoNotFoundError(str(note.video_id))
        return note, video

    async def mark_processing(self, note_id: UUID) -> VideoNote:
        """Advance a pending row to processing."""
        await self.get_by_id(note_id)
        return await self._note_repo.update_status(note_id, status=STATUS_PROCESSING)

    async def mark_ready(self, note_id: UUID, blob_url: str) -> VideoNote:
        """Publish a finished PDF."""
        await self.get_by_id(note_id)
        return await self._note_repo.update_status(note_id, status=STATUS_READY, blob_url=blob_url)

    async def mark_failed(self, note_id: UUID, error: str) -> VideoNote:
        """Record a worker-reported, user-safe failure."""
        await self.get_by_id(note_id)
        return await self._note_repo.update_status(note_id, status=STATUS_FAILED, error=error)

    async def _expire_if_stale(self, note: VideoNote) -> bool:
        """Fail an active row past the worker deadline; report whether it was."""
        cutoff = datetime.now(UTC) - timedelta(seconds=NOTES_STALE_SECONDS)
        if note.updated_at >= cutoff:
            return False
        await self._note_repo.update_status(note.id, status=STATUS_FAILED, error=NOTES_STALE_ERROR)
        logger.warning("Expired stale lecture notes request", note_id=note.id)
        return True

    async def _fail_ready(self, video_id: UUID) -> None:
        """Retire a cached PDF so regeneration can queue a fresh request."""
        ready = await self._note_repo.get_ready(video_id)
        if ready is not None:
            await self._note_repo.update_status(
                ready.id,
                status=STATUS_FAILED,
                error=NOTES_SUPERSEDED_ERROR,
            )
            logger.info("Retired cached lecture notes for regeneration", note_id=ready.id)

    async def _create_or_reuse(self, video_id: UUID) -> VideoNote:
        """Create a pending row, resolving a concurrent-create race."""
        try:
            return await self._note_repo.create(video_id=video_id)
        except IntegrityError:
            await self._note_repo.session.rollback()
            active = await self._note_repo.get_active(video_id)
            if active is not None:
                return active
            raise
