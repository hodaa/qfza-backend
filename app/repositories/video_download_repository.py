"""Repository for offloaded video download rows."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import VideoDownload

logger = get_logger(__name__)


class VideoDownloadRepository:
    """Repository for VideoDownload operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        video_id: UUID,
        start_seconds: int | None,
        end_seconds: int | None,
        height: int | None,
    ) -> VideoDownload:
        """Create a pending download row."""
        download = VideoDownload(
            video_id=video_id,
            status="pending",
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            height=height,
        )
        self.session.add(download)
        await self.session.flush()
        logger.info("Created video download", download_id=download.id, video_id=video_id)
        return download

    async def get_by_id(self, download_id: UUID) -> VideoDownload | None:
        """Get a download by primary key."""
        result = await self.session.execute(
            select(VideoDownload).where(VideoDownload.id == download_id)
        )
        return result.scalar_one_or_none()

    async def get_for_video(self, download_id: UUID, video_id: UUID) -> VideoDownload | None:
        """Get a download scoped to its video, so another video's id never resolves."""
        result = await self.session.execute(
            select(VideoDownload).where(
                VideoDownload.id == download_id,
                VideoDownload.video_id == video_id,
            )
        )
        return result.scalar_one_or_none()

    async def update_status(
        self,
        download_id: UUID,
        *,
        status: str,
        blob_url: str | None = None,
        error: str | None = None,
        consumed: bool = False,
    ) -> VideoDownload:
        """Set a download's status plus whatever that status carries."""
        download = await self.get_by_id(download_id)
        if not download:
            raise ValueError(f"VideoDownload {download_id} not found")
        download.status = status
        if blob_url is not None:
            download.blob_url = blob_url
        if error is not None:
            download.error = error
        if consumed:
            download.consumed_at = func.now()
        await self.session.flush()
        logger.info("Updated video download", download_id=download_id, status=status)
        return download

    async def list_stale_active(self, cutoff: datetime, limit: int) -> list[VideoDownload]:
        """Rows a died worker left in pending/processing past the cutoff."""
        result = await self.session.execute(
            select(VideoDownload)
            .where(
                VideoDownload.status.in_(("pending", "processing")),
                VideoDownload.updated_at < cutoff,
            )
            .order_by(VideoDownload.updated_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_expired(self, cutoff: datetime, limit: int) -> list[VideoDownload]:
        """Terminal rows past retention, so their blobs and rows can be removed."""
        result = await self.session.execute(
            select(VideoDownload)
            .where(
                VideoDownload.status.in_(("ready", "failed", "consumed")),
                VideoDownload.updated_at < cutoff,
            )
            .order_by(VideoDownload.updated_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def delete_ids(self, download_ids: list[UUID]) -> None:
        """Remove rows by id."""
        await self.session.execute(delete(VideoDownload).where(VideoDownload.id.in_(download_ids)))
