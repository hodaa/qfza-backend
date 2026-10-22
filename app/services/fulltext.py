"""Full-text catalog search over transcribed videos."""

from app.repositories import VideoRepository
from app.schemas import VideoSearchResult

_MAX_FTS_LIMIT = 50


class FullTextSearchService:
    """Search the video catalog by transcript full-text relevance."""

    def __init__(self, video_repo: VideoRepository) -> None:
        self.video_repo = video_repo

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[VideoSearchResult]:
        """Run a websearch full-text query against transcribed videos.

        Args:
            query: Raw user query (stemming, phrase quotes and OR apply).
            limit: Maximum number of results to return.

        Returns:
            Ranked list of matching videos with headline snippets.
        """
        normalized = query.strip()
        if not normalized:
            return []
        rows = await self.video_repo.search_full_text(normalized, limit=min(limit, _MAX_FTS_LIMIT))
        return [
            VideoSearchResult(
                video_id=row["video_id"],
                youtube_video_id=row["youtube_video_id"],
                youtube_url=row["video_url"],
                title=row["title"],
                duration_seconds=row["duration_seconds"],
                snippet=row["snippet"],
                rank=float(row["rank"]),
            )
            for row in rows
        ]
