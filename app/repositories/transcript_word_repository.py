from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import TranscriptWord

logger = get_logger(__name__)


class TranscriptWordRepository:
    """Repository for TranscriptWord operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_create(self, words: list[TranscriptWord]) -> None:
        """Bulk create transcript words."""
        self.session.add_all(words)
        await self.session.flush()
        logger.info("Bulk created transcript words", count=len(words))

    async def delete_by_video_id(self, video_id: UUID) -> None:
        """Delete all transcript words for a video."""
        await self.session.execute(
            delete(TranscriptWord).where(TranscriptWord.video_id == video_id)
        )
        await self.session.flush()

    async def search_exact_phrase(
        self,
        video_id: UUID,
        normalized_words: list[str],
    ) -> list[TranscriptWord]:
        """Return candidate words matching any phrase term (see SearchService)."""
        if not normalized_words:
            return []
        result = await self.session.execute(
            select(TranscriptWord)
            .where(TranscriptWord.video_id == video_id)
            .where(TranscriptWord.word.in_(normalized_words))
            .order_by(TranscriptWord.word_index)
        )
        return list(result.scalars().all())

    async def get_by_index_range(
        self,
        video_id: UUID,
        start_index: int,
        end_index: int,
    ) -> Sequence[TranscriptWord]:
        """Get transcript words within a word_index range (inclusive)."""
        result = await self.session.execute(
            select(TranscriptWord)
            .where(TranscriptWord.video_id == video_id)
            .where(TranscriptWord.word_index >= start_index)
            .where(TranscriptWord.word_index <= end_index)
            .order_by(TranscriptWord.word_index)
        )
        return result.scalars().all()

    async def list_all_for_video(self, video_id: UUID) -> Sequence[TranscriptWord]:
        """Get every word of a video's transcript, in transcript order."""
        result = await self.session.execute(
            select(TranscriptWord)
            .where(TranscriptWord.video_id == video_id)
            .order_by(TranscriptWord.word_index)
        )
        return result.scalars().all()
