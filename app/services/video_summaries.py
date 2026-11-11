"""AI-generated highlight videos: the request ledger.

The backend never downloads or edits media. It queues a ``video_summaries``
row, the worker scores the transcript, cuts and concatenates the selected
moments, uploads the finished MP4 to blob storage, and reports the blob URL
back. A ready row is reused so a second click does not pay for the same render
twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import SummaryNotFoundError, VideoNotFoundError, VideoNotTranscribedError
from app.core.logging import get_logger
from app.models import Video, VideoSummary
from app.repositories import VideoRepository, VideoSummaryRepository

logger = get_logger(__name__)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

#: A worker killed mid-render leaves a row active; past this age it is failed
#: so the next click can start a fresh request.
SUMMARY_STALE_SECONDS = 1800

#: User-safe failure recorded for a request the sweep gave up on.
SUMMARY_STALE_ERROR = "Video-summary generation timed out. Please try again."

#: Recorded on a cached summary when the user asks for a fresh render.
SUMMARY_SUPERSEDED_ERROR = "Superseded by a newer video-summary request."


async def _require_video(video_repo: VideoRepository, youtube_video_id: str) -> Video:
    """Resolve a video by its source id, or raise 404."""
    video = await video_repo.get_by_video_id_lite(youtube_video_id)
    if video is None:
        raise VideoNotFoundError(youtube_video_id)
    return video


class VideoSummaryService:
    """Owns video-summary rows from queueing to the cached blob URL."""

    def __init__(self, video_repo: VideoRepository, summary_repo: VideoSummaryRepository) -> None:
        """Wire the service to its repositories."""
        self._video_repo = video_repo
        self._summary_repo = summary_repo

    async def create(self, youtube_video_id: str, *, regenerate: bool = False) -> VideoSummary:
        """Return a cached/active row, or queue a new video-summary request."""
        video = await _require_video(self._video_repo, youtube_video_id)
        if not video.transcribed_at:
            raise VideoNotTranscribedError(youtube_video_id)

        active = await self._summary_repo.get_active(video.id)
        if active is not None and not await self._expire_if_stale(active):
            return active
        if regenerate:
            await self._fail_ready(video.id)
            return await self._create_or_reuse(video.id)

        ready = await self._summary_repo.get_ready(video.id)
        if ready is not None:
            return ready
        return await self._create_or_reuse(video.id)

    async def get(self, youtube_video_id: str, summary_id: UUID) -> VideoSummary:
        """Return one summary scoped to its video, or 404."""
        video = await _require_video(self._video_repo, youtube_video_id)
        summary = await self._summary_repo.get_for_video(summary_id, video.id)
        if summary is None:
            raise SummaryNotFoundError(str(summary_id))
        return summary

    async def get_by_id(self, summary_id: UUID) -> VideoSummary:
        """Return a summary by id, or 404."""
        summary = await self._summary_repo.get_by_id(summary_id)
        if summary is None:
            raise SummaryNotFoundError(str(summary_id))
        return summary

    async def get_for_worker(self, summary_id: UUID) -> tuple[VideoSummary, Video]:
        """Return a summary and its video for the worker's fetch."""
        summary = await self.get_by_id(summary_id)
        video = await self._video_repo.get_by_id_lite(summary.video_id)
        if video is None:
            raise VideoNotFoundError(str(summary.video_id))
        return summary, video

    async def mark_processing(self, summary_id: UUID) -> VideoSummary:
        """Advance a pending row to processing."""
        await self.get_by_id(summary_id)
        return await self._summary_repo.update_status(summary_id, status=STATUS_PROCESSING)

    async def mark_ready(self, summary_id: UUID, blob_url: str) -> VideoSummary:
        """Publish a finished highlight video."""
        await self.get_by_id(summary_id)
        return await self._summary_repo.update_status(
            summary_id,
            status=STATUS_READY,
            blob_url=blob_url,
        )

    async def mark_failed(self, summary_id: UUID, error: str) -> VideoSummary:
        """Record a worker-reported, user-safe failure."""
        await self.get_by_id(summary_id)
        return await self._summary_repo.update_status(
            summary_id,
            status=STATUS_FAILED,
            error=error,
        )

    async def _expire_if_stale(self, summary: VideoSummary) -> bool:
        """Fail an active row past the worker deadline; report whether it was."""
        cutoff = datetime.now(UTC) - timedelta(seconds=SUMMARY_STALE_SECONDS)
        if summary.updated_at >= cutoff:
            return False
        await self._summary_repo.update_status(
            summary.id,
            status=STATUS_FAILED,
            error=SUMMARY_STALE_ERROR,
        )
        logger.warning("Expired stale video summary request", summary_id=summary.id)
        return True

    async def _fail_ready(self, video_id: UUID) -> None:
        """Retire a cached summary so regeneration can queue a fresh request."""
        ready = await self._summary_repo.get_ready(video_id)
        if ready is not None:
            await self._summary_repo.update_status(
                ready.id,
                status=STATUS_FAILED,
                error=SUMMARY_SUPERSEDED_ERROR,
            )
            logger.info("Retired cached video summary for regeneration", summary_id=ready.id)

    async def _create_or_reuse(self, video_id: UUID) -> VideoSummary:
        """Create a pending row, resolving a concurrent-create race."""
        try:
            return await self._summary_repo.create(video_id=video_id)
        except IntegrityError:
            await self._summary_repo.session.rollback()
            active = await self._summary_repo.get_active(video_id)
            if active is not None:
                return active
            raise
