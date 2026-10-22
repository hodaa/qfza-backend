"""Unit tests for FullTextSearchService."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.repositories import VideoRepository
from app.services.fulltext import FullTextSearchService


@pytest.fixture
def mock_video_repo() -> MagicMock:
    """Create a mock VideoRepository."""
    repo = MagicMock(spec=VideoRepository)
    repo.search_full_text = AsyncMock()
    return repo


@pytest.fixture
def fulltext_service(mock_video_repo: MagicMock) -> FullTextSearchService:
    """Create FullTextSearchService with a mock repository."""
    return FullTextSearchService(mock_video_repo)


def _row(
    *,
    video_id,
    youtube_video_id: str = "abc123",
    title: str | None = "A Title",
    snippet: str | None = "<mark>hello</mark> world",
    rank: float = 1.0,
) -> dict:
    """Build a fake repository row mapping."""
    return {
        "video_id": video_id,
        "youtube_video_id": youtube_video_id,
        "video_url": f"https://www.youtube.com/watch?v={youtube_video_id}",
        "title": title,
        "duration_seconds": 100,
        "snippet": snippet,
        "rank": rank,
    }


class TestFullTextSearchService:
    """Tests for catalog full-text search."""

    @pytest.mark.asyncio
    async def test_returns_mapped_results(
        self,
        fulltext_service: FullTextSearchService,
        mock_video_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        mock_video_repo.search_full_text.return_value = [_row(video_id=video_id)]

        results = await fulltext_service.search("hello world")

        mock_video_repo.search_full_text.assert_awaited_once_with("hello world", limit=10)
        assert len(results) == 1
        result = results[0]
        assert result.video_id == video_id
        assert result.youtube_video_id == "abc123"
        assert result.title == "A Title"
        assert result.snippet == "<mark>hello</mark> world"
        assert result.rank == 1.0

    @pytest.mark.asyncio
    async def test_ranks_by_relevance_order(
        self,
        fulltext_service: FullTextSearchService,
        mock_video_repo: MagicMock,
    ) -> None:
        high = uuid4()
        low = uuid4()
        mock_video_repo.search_full_text.return_value = [
            _row(video_id=high, rank=0.8),
            _row(video_id=low, rank=0.3),
        ]

        results = await fulltext_service.search("hello", limit=50)

        assert [r.video_id for r in results] == [high, low]

    @pytest.mark.asyncio
    async def test_empty_query_skips_repository(
        self,
        fulltext_service: FullTextSearchService,
        mock_video_repo: MagicMock,
    ) -> None:
        results = await fulltext_service.search("   ")

        assert results == []
        mock_video_repo.search_full_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whitespace_padded_query_is_normalized(
        self,
        fulltext_service: FullTextSearchService,
        mock_video_repo: MagicMock,
    ) -> None:
        mock_video_repo.search_full_text.return_value = []

        await fulltext_service.search("  hello  ")

        mock_video_repo.search_full_text.assert_awaited_once_with("hello", limit=10)

    @pytest.mark.asyncio
    async def test_no_results_returns_empty(
        self,
        fulltext_service: FullTextSearchService,
        mock_video_repo: MagicMock,
    ) -> None:
        mock_video_repo.search_full_text.return_value = []

        results = await fulltext_service.search("nonexistent")

        assert results == []
