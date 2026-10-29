"""Resolve a pasted video link and prepare its transcript on demand."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import VideoNotFoundError
from app.core.logging import get_logger
from app.models import Job, Video
from app.repositories import VideoRepository
from app.services.job import JobService
from app.sources import identify_video

logger = get_logger(__name__)

DEFAULT_LANGUAGE = "en"


class VideoWorkspaceService:
    """Turn a source link into a stored video and queue transcription lazily."""

    def __init__(
        self,
        video_repo: VideoRepository,
        job_service: JobService,
    ) -> None:
        """Wire the service to the repositories it reads and writes."""
        self._video_repo = video_repo
        self._job_service = job_service

    async def resolve(self, video_url: str) -> Video:
        """Return the stored video for a link, creating the row when it is new."""
        video_ref = identify_video(video_url)
        video = await self._video_repo.get_by_video_id_lite(video_ref.source_id)
        if video is not None:
            return video
        return await self._create_video(video_ref.canonical_url, video_ref.source_id)

    async def prepare(self, youtube_video_id: str) -> tuple[Video, Job | None, bool]:
        """Return the video, its job, and whether the job needs dispatching."""
        video = await self._video_repo.get_by_video_id_lite(youtube_video_id)
        if video is None:
            raise VideoNotFoundError(youtube_video_id)
        if video.transcribed_at is not None:
            return video, None, False
        existing = await self._job_service.job_repo.get_in_flight_by_video_id(video.id)
        if existing is not None:
            return video, existing, False
        job = await self._job_service.create_or_get_job(video.id)
        return video, job, True

    async def _create_video(self, video_url: str, video_id: str) -> Video:
        """Create a video row, resolving a concurrent-create race."""
        try:
            return await self._video_repo.create(
                video_url=video_url,
                video_id=video_id,
                language=DEFAULT_LANGUAGE,
            )
        except IntegrityError:
            await self._video_repo.session.rollback()
            video = await self._video_repo.get_by_video_id_lite(video_id)
            if video is None:
                raise
            logger.info("Reused video created by a concurrent request", video_id=video_id)
            return video


__all__ = ["VideoWorkspaceService"]
