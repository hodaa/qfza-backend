"""Integration tests for catalog full-text search (PostgreSQL FTS)."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import func, update

from app.models import Video

_TEXT_A = "hello world this is a test hello again world test"
_TEXT_B = "welcome to the show please stay tuned"


async def _seed_transcribed_video(
    db_session,
    *,
    youtube_video_id: str,
    title: str,
    transcript: str,
) -> Video:
    """Create a transcribed video with a real to_tsvector vector."""
    video = Video(
        video_url=f"https://www.youtube.com/watch?v={youtube_video_id}",
        video_id=youtube_video_id,
        title=title,
        language="en",
    )
    db_session.add(video)
    await db_session.flush()
    await db_session.execute(
        update(Video)
        .where(Video.id == video.id)
        .values(
            transcript=transcript,
            transcript_tsvector=func.to_tsvector("english", transcript),
            transcribed_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    await db_session.refresh(video)
    return video


async def _seed_untranscribed_video(db_session, *, youtube_video_id: str) -> Video:
    """Create a video with no transcript (no tsvector)."""
    video = Video(
        video_url=f"https://www.youtube.com/watch?v={youtube_video_id}",
        video_id=youtube_video_id,
        title="Untranscribed",
        language="en",
    )
    db_session.add(video)
    await db_session.flush()
    return video


@pytest.mark.asyncio
async def test_catalog_search_returns_only_matching_videos(
    client: AsyncClient,
    db_session,
) -> None:
    """Query 'hello' must return the matching video and skip others."""
    match = await _seed_transcribed_video(
        db_session, youtube_video_id="fts1abcdefgh", title="Hello Video", transcript=_TEXT_A
    )
    await _seed_transcribed_video(
        db_session, youtube_video_id="fts2abcdefgh", title="Other Video", transcript=_TEXT_B
    )
    await _seed_untranscribed_video(db_session, youtube_video_id="fts3abcdefgh")

    response = await client.get("/api/videos/search", params={"q": "hello"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "found"
    assert len(data["results"]) == 1
    result = data["results"][0]
    assert result["video_id"] == str(match.id)
    assert result["title"] == "Hello Video"
    assert result["rank"] > 0
    assert "<mark>" in result["snippet"]


@pytest.mark.asyncio
async def test_catalog_search_applies_or_semantics(
    client: AsyncClient,
    db_session,
) -> None:
    """Explicit websearch OR: query matching either transcript returns both."""
    first = await _seed_transcribed_video(
        db_session, youtube_video_id="fts4abcdefgh", title="Hello Video", transcript=_TEXT_A
    )
    second = await _seed_transcribed_video(
        db_session, youtube_video_id="fts5abcdefgh", title="Welcome Video", transcript=_TEXT_B
    )

    response = await client.get("/api/videos/search", params={"q": "hello OR welcome"})

    data = response.json()
    assert data["status"] == "found"
    assert len(data["results"]) == 2
    ids = {r["video_id"] for r in data["results"]}
    assert ids == {str(first.id), str(second.id)}


@pytest.mark.asyncio
async def test_catalog_search_respects_limit(
    client: AsyncClient,
    db_session,
) -> None:
    """Two matching videos with limit=1 returns one result."""
    await _seed_transcribed_video(
        db_session, youtube_video_id="fts6abcdefgh", title="Hello One", transcript=_TEXT_A
    )
    await _seed_transcribed_video(
        db_session, youtube_video_id="fts7abcdefgh", title="Hello Two", transcript=_TEXT_B
    )

    response = await client.get("/api/videos/search", params={"q": "hello OR welcome", "limit": 1})

    assert response.status_code == 200
    assert len(response.json()["results"]) == 1


@pytest.mark.asyncio
async def test_catalog_search_no_match_returns_not_found(
    client: AsyncClient,
    db_session,
) -> None:
    """A query matching nothing returns not_found with empty results."""
    await _seed_transcribed_video(
        db_session, youtube_video_id="fts8abcdefgh", title="Hello Video", transcript=_TEXT_A
    )

    response = await client.get("/api/videos/search", params={"q": "supercalifragilistic"})

    data = response.json()
    assert data["status"] == "not_found"
    assert data["results"] == []


@pytest.mark.asyncio
async def test_catalog_search_drops_stopwords(
    client: AsyncClient,
    db_session,
) -> None:
    """A stopword-only query has no lexemes and returns not_found."""
    await _seed_transcribed_video(
        db_session, youtube_video_id="fts9abcdefgh", title="Hello Video", transcript=_TEXT_A
    )

    response = await client.get("/api/videos/search", params={"q": "the and of"})

    data = response.json()
    assert data["status"] == "not_found"
    assert data["results"] == []
