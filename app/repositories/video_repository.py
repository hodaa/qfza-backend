from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, noload

from app.core.logging import get_logger
from app.models import Video

logger = get_logger(__name__)

_FTS_CONFIG = "english"
_FTS_SNIPPET_OPTIONS = "StartSel=<mark>, StopSel=</mark>, MinWords=12, MaxWords=20"


class VideoRepository:
    """Repository for Video operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, video_id: UUID) -> Video | None:
        """Get video by primary key."""
        result = await self.session.execute(select(Video).where(Video.id == video_id))
        return result.scalar_one_or_none()

    async def get_by_id_lite(self, video_id: UUID) -> Video | None:
        """Get video by primary key, skipping the heavy transcript columns."""
        result = await self.session.execute(
            select(Video)
            .where(Video.id == video_id)
            .options(
                defer(Video.transcript),
                defer(Video.transcript_tsvector),
                noload(Video.transcript_words),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_video_id(self, video_id: str) -> Video | None:
        """Get video by YouTube video ID."""
        result = await self.session.execute(select(Video).where(Video.video_id == video_id))
        return result.scalar_one_or_none()

    async def get_by_video_id_lite(self, video_id: str) -> Video | None:
        """Get video by YouTube video ID, skipping the heavy transcript columns."""
        result = await self.session.execute(
            select(Video)
            .where(Video.video_id == video_id)
            .options(
                defer(Video.transcript),
                defer(Video.transcript_tsvector),
                noload(Video.transcript_words),
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        youtube_url: str,
        video_id: str,
        language: str,
        title: str | None = None,
        duration_seconds: int | None = None,
    ) -> Video:
        """Create a new video record."""
        video = Video(
            youtube_url=youtube_url,
            video_id=video_id,
            title=title,
            duration_seconds=duration_seconds,
            language=language,
        )
        self.session.add(video)
        await self.session.flush()
        logger.info("Created video", video_id=video.id, youtube_video_id=video_id)
        return video

    async def update_metadata(
        self,
        video_id: UUID,
        *,
        title: str,
        duration_seconds: int,
    ) -> Video:
        """Update video metadata from the media provider."""
        video = await self.session.get(Video, video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found")
        video.title = title
        video.duration_seconds = duration_seconds
        await self.session.flush()
        logger.info("Updated video metadata", video_id=video_id)
        return video

    async def update_transcript(
        self,
        video_id: UUID,
        *,
        transcript: str,
        language: str | None = None,
        provider: str = "",
    ) -> Video:
        """Store transcript text, language, provider and full-text vector.

        An empty transcript (no speech detected) is stored as a NULL row: the
        video counts as transcribed so repeat searches never re-queue a job,
        but it stays out of full-text search and reads back as ``null`` so the
        UI can tell "no speech" from "no match".
        """
        video = await self.session.get(Video, video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found")
        video.transcript = transcript if transcript and transcript.strip() else None
        video.language = language
        video.provider = provider
        video.transcript_tsvector = (
            func.to_tsvector("english", transcript) if video.transcript is not None else None
        )
        video.transcribed_at = func.now()
        await self.session.flush()
        logger.info("Updated video transcript", video_id=video_id)
        return video

    async def update_available_heights(self, video_id: UUID, heights: list[int]) -> Video:
        """Cache the H.264 quality ladder the worker probed for a video."""
        video = await self.session.get(Video, video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found")
        video.available_heights = heights
        video.formats_refreshed_at = func.now()
        await self.session.flush()
        logger.info("Updated video format heights", video_id=video_id, heights=heights)
        return video

    async def has_speech_text(self, video_id: UUID) -> bool:
        """True when the video's stored transcript contains real speech."""
        result = await self.session.execute(select(Video.transcript).where(Video.id == video_id))
        transcript = result.scalar_one_or_none()
        return bool(transcript and transcript.strip())

    async def is_transcribed(self, video_id: UUID) -> bool:
        """Check if video has been transcribed."""
        result = await self.session.execute(
            select(Video.transcribed_at).where(Video.id == video_id)
        )
        transcribed_at = result.scalar_one_or_none()
        return transcribed_at is not None

    async def search_full_text(
        self,
        query: str,
        *,
        limit: int = 10,
        config: str = _FTS_CONFIG,
    ) -> Sequence[RowMapping]:
        """Search transcribed videos by transcript full-text relevance.

        Uses the GIN index on ``transcript_tsvector``; results are ranked by
        ``ts_rank`` and carry a ``ts_headline`` snippet around the first hit.
        """
        tsquery = func.websearch_to_tsquery(config, query)
        tsvector = Video.transcript_tsvector
        rank = func.ts_rank(tsvector, tsquery)
        stmt = (
            select(
                Video.id.label("video_id"),
                Video.video_id.label("youtube_video_id"),
                Video.youtube_url,
                Video.title,
                Video.duration_seconds,
                rank.label("rank"),
                func.ts_headline(
                    config,
                    Video.transcript,
                    tsquery,
                    _FTS_SNIPPET_OPTIONS,
                ).label("snippet"),
            )
            .where(tsvector.op("@@", is_comparison=True)(tsquery))
            .order_by(rank.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.mappings().all())
