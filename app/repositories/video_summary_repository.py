"""Repository for AI-generated video-summary rows."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import VideoSummary

logger = get_logger(__name__)


class VideoSummaryRepository:
    """Repository for VideoSummary operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, video_id: UUID) -> VideoSummary:
        """Create a pending video-summary row."""
        summary = VideoSummary(video_id=video_id, status="pending")
        self.session.add(summary)
        await self.session.flush()
        logger.info("Created video summary request", summary_id=summary.id, video_id=video_id)
        return summary

    async def get_by_id(self, summary_id: UUID) -> VideoSummary | None:
        """Get a video-summary row by primary key."""
        result = await self.session.execute(
            select(VideoSummary).where(VideoSummary.id == summary_id)
        )
        return result.scalar_one_or_none()

    async def get_for_video(self, summary_id: UUID, video_id: UUID) -> VideoSummary | None:
        """Get a row scoped to its video."""
        result = await self.session.execute(
            select(VideoSummary).where(
                VideoSummary.id == summary_id,
                VideoSummary.video_id == video_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_active(self, video_id: UUID) -> VideoSummary | None:
        """Return the video's pending or processing row, if any."""
        result = await self.session.execute(
            select(VideoSummary).where(
                VideoSummary.video_id == video_id,
                VideoSummary.status.in_(("pending", "processing")),
            )
        )
        return result.scalar_one_or_none()

    async def get_ready(self, video_id: UUID) -> VideoSummary | None:
        """Return the video's most recent ready row."""
        result = await self.session.execute(
            select(VideoSummary)
            .where(VideoSummary.video_id == video_id, VideoSummary.status == "ready")
            .order_by(VideoSummary.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def update_status(
        self,
        summary_id: UUID,
        *,
        status: str,
        blob_url: str | None = None,
        error: str | None = None,
    ) -> VideoSummary:
        """Set a row's status plus whatever that status carries."""
        summary = await self.get_by_id(summary_id)
        if summary is None:
            raise ValueError(f"VideoSummary {summary_id} not found")
        summary.status = status
        if blob_url is not None:
            summary.blob_url = blob_url
        if error is not None:
            summary.error = error
        await self.session.flush()
        logger.info("Updated video summary request", summary_id=summary_id, status=status)
        return summary

    async def list_stale_active(self, cutoff: datetime, limit: int) -> list[VideoSummary]:
        """Rows a died worker left active past the cutoff."""
        result = await self.session.execute(
            select(VideoSummary)
            .where(
                VideoSummary.status.in_(("pending", "processing")),
                VideoSummary.updated_at < cutoff,
            )
            .order_by(VideoSummary.updated_at)
            .limit(limit)
        )
        return list(result.scalars().all())
