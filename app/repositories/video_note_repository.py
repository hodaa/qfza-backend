"""Repository for AI-generated lecture-notes rows."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import VideoNote

logger = get_logger(__name__)


class VideoNoteRepository:
    """Repository for VideoNote operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, video_id: UUID) -> VideoNote:
        """Create a pending lecture-notes row."""
        note = VideoNote(video_id=video_id, status="pending")
        self.session.add(note)
        await self.session.flush()
        logger.info("Created lecture notes request", note_id=note.id, video_id=video_id)
        return note

    async def get_by_id(self, note_id: UUID) -> VideoNote | None:
        """Get a lecture-notes row by primary key."""
        result = await self.session.execute(select(VideoNote).where(VideoNote.id == note_id))
        return result.scalar_one_or_none()

    async def get_for_video(self, note_id: UUID, video_id: UUID) -> VideoNote | None:
        """Get a row scoped to its video."""
        result = await self.session.execute(
            select(VideoNote).where(VideoNote.id == note_id, VideoNote.video_id == video_id)
        )
        return result.scalar_one_or_none()

    async def get_active(self, video_id: UUID) -> VideoNote | None:
        """Return the video's pending or processing row, if any."""
        result = await self.session.execute(
            select(VideoNote).where(
                VideoNote.video_id == video_id,
                VideoNote.status.in_(("pending", "processing")),
            )
        )
        return result.scalar_one_or_none()

    async def get_ready(self, video_id: UUID) -> VideoNote | None:
        """Return the video's most recent ready row."""
        result = await self.session.execute(
            select(VideoNote)
            .where(VideoNote.video_id == video_id, VideoNote.status == "ready")
            .order_by(VideoNote.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def update_status(
        self,
        note_id: UUID,
        *,
        status: str,
        blob_url: str | None = None,
        error: str | None = None,
    ) -> VideoNote:
        """Set a row's status plus whatever that status carries."""
        note = await self.get_by_id(note_id)
        if note is None:
            raise ValueError(f"VideoNote {note_id} not found")
        note.status = status
        if blob_url is not None:
            note.blob_url = blob_url
        if error is not None:
            note.error = error
        await self.session.flush()
        logger.info("Updated lecture notes request", note_id=note_id, status=status)
        return note

    async def list_stale_active(self, cutoff: datetime, limit: int) -> list[VideoNote]:
        """Rows a died worker left active past the cutoff."""
        result = await self.session.execute(
            select(VideoNote)
            .where(
                VideoNote.status.in_(("pending", "processing")),
                VideoNote.updated_at < cutoff,
            )
            .order_by(VideoNote.updated_at)
            .limit(limit)
        )
        return list(result.scalars().all())
