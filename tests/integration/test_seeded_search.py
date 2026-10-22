"""Integration test for seeded full-path search (NN-5 survival test)."""

from datetime import UTC
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.models import Job, TranscriptWord, Video
from tests.utils.job_driver import (
    FAKE_TRANSCRIPT_TEXT,
    FAKE_TRANSCRIPT_TSVECTOR,
    FAKE_TRANSCRIPT_WORDS,
    JobDriver,
)


@pytest.mark.asyncio
async def test_seeded_video_exact_phrase_search(
    client: AsyncClient,
    db_session,
) -> None:
    """
    NN-5 Survival Test: Seed a transcribed video with transcript_words + tsvector,
    then assert exact multi-word phrase timestamps through GET /api/video/{id}/search.
    Times MUST come from transcript_words rows (real start/end_time).
    """
    # Create a video with transcript
    from datetime import datetime

    video = Video(
        video_url="https://www.youtube.com/watch?v=test123",
        video_id="test123",
        title="Test Video",
        duration_seconds=100,
        language="en",
        transcript=FAKE_TRANSCRIPT_TEXT,
        transcript_tsvector=FAKE_TRANSCRIPT_TSVECTOR,
        transcribed_at=datetime.now(UTC),
    )
    db_session.add(video)
    await db_session.flush()

    # Add transcript words
    for word_data in FAKE_TRANSCRIPT_WORDS:
        word = TranscriptWord(video_id=video.id, **word_data)
        db_session.add(word)

    await db_session.flush()
    await db_session.refresh(video)

    # Search for exact phrase "hello world" (indices 0-1, start_time=0.0)
    response = await client.get(f"/api/video/{video.id}/search?keyword=hello world")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "found"
    assert len(data["results"]) == 1

    result = data["results"][0]
    assert result["timestamp"] == "00:00"
    assert result["progress_seconds"] == 0.0
    assert "hello world" in result["text_snippet"].lower()

    # Search for exact phrase "this is a test" (indices 2-5, start_time=1.0)
    response = await client.get(f"/api/video/{video.id}/search?keyword=this is a test")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "found"
    assert len(data["results"]) == 1

    result = data["results"][0]
    assert result["timestamp"] == "00:01"
    assert result["progress_seconds"] == 1.0

    # Search for "hello again" (indices 6-7, start_time=2.0)
    response = await client.get(f"/api/video/{video.id}/search?keyword=hello again")
    assert response.status_code == 200

    data = response.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["progress_seconds"] == 2.0

    # Search for "world test" - NOT consecutive (indices 1,5 and 8,9)
    response = await client.get(f"/api/video/{video.id}/search?keyword=world test")
    assert response.status_code == 200

    data = response.json()
    # Should find at index 8-9 (world at 8, test at 9)
    assert len(data["results"]) == 1
    assert data["results"][0]["progress_seconds"] == 3.0

    # Search for non-existent phrase
    response = await client.get(f"/api/video/{video.id}/search?keyword=nonexistent phrase")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "not_found"
    assert data["results"] == []

    # Case insensitive search
    response = await client.get(f"/api/video/{video.id}/search?keyword=HELLO WORLD")
    assert response.status_code == 200
    assert len(response.json()["results"]) == 1


@pytest.mark.asyncio
async def test_large_transcript_search(
    client: AsyncClient,
    db_session,
) -> None:
    """
    Test that a large transcript returns all matches.
    Seeds 1000 words and asserts every match is returned.
    """
    video = Video(
        video_url="https://www.youtube.com/watch?v=perftest123",
        video_id="perftest123",
        title="Performance Test",
        duration_seconds=100,
        language="en",
        transcript="test " * 1000,  # Large transcript
        transcript_tsvector="'test':1,2,3,4,5",
    )
    from datetime import datetime

    video.transcribed_at = datetime.now(UTC)
    db_session.add(video)
    await db_session.flush()

    # Add many transcript words
    for i in range(1000):
        word = TranscriptWord(
            video_id=video.id,
            word_index=i,
            word="test",
            start_time=float(i),
            end_time=float(i + 0.5),
        )
        db_session.add(word)

    await db_session.flush()
    await db_session.refresh(video)

    response = await client.get(f"/api/video/{video.id}/search?keyword=test")

    assert response.status_code == 200
    assert len(response.json()["results"]) == 1000


@pytest.mark.asyncio
async def test_full_job_lifecycle_with_driver(
    client: AsyncClient,
    db_session,
) -> None:
    """
    Test full job lifecycle using JobDriver:
    POST /api/search -> 202 with job_id
    GET /api/status/{job_id} -> pending
    advance to processing -> status=processing
    complete with fake transcript -> status=completed with results
    """
    # Create video without transcript
    video = Video(
        video_url="https://www.youtube.com/watch?v=lifecycle12",
        video_id="lifecycle12",
        title="Lifecycle Test",
    )
    db_session.add(video)
    await db_session.flush()

    # Initial search - cache miss
    response = await client.post(
        "/api/search",
        json={"youtube_url": video.video_url, "keyword": "hello world"},
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "processing"
    job_id = data["job_id"]

    # Check initial status
    response = await client.get(f"/api/status/{job_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"

    # Advance to processing
    driver = JobDriver(db_session)
    await driver.advance_to_processing(UUID(job_id))

    response = await client.get(f"/api/status/{job_id}")
    assert response.json()["status"] == "processing"

    # Complete with fake transcript
    await driver.complete_with_fake_transcript(UUID(job_id))

    response = await client.get(f"/api/status/{job_id}")
    data = response.json()
    assert data["status"] == "completed"
    assert data["progress"] == 100


@pytest.mark.asyncio
async def test_failed_job_returns_user_safe_error(
    client: AsyncClient,
    db_session,
) -> None:
    """
    Test that failed job returns user-safe error string (NN-4).
    """
    video = Video(
        video_url="https://www.youtube.com/watch?v=fail123test",
        video_id="fail123test",
        title="Fail Test",
    )
    db_session.add(video)
    await db_session.flush()

    response = await client.post(
        "/api/search",
        json={"youtube_url": video.video_url, "keyword": "test"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    driver = JobDriver(db_session)
    await driver.fail_job(UUID(job_id), "Transcription failed after 3 retries")

    response = await client.get(f"/api/status/{job_id}")
    data = response.json()
    assert data["status"] == "failed"
    assert data["error"] == "Transcription failed after 3 retries"
    assert "traceback" not in data["error"].lower()
    assert "exception" not in data["error"].lower()


@pytest.mark.asyncio
async def test_resubmit_after_failed_creates_new_job(
    client: AsyncClient,
    db_session,
) -> None:
    """
    Test that re-submitting after a failed job creates a NEW job.
    """
    video = Video(
        video_url="https://www.youtube.com/watch?v=resubmit123",
        video_id="resubmit123",
        title="Resubmit Test",
    )
    db_session.add(video)
    await db_session.flush()

    # First submission
    response1 = await client.post(
        "/api/search",
        json={"youtube_url": video.video_url, "keyword": "test"},
    )
    assert response1.status_code == 202
    job_id_1 = response1.json()["job_id"]

    # Fail the job
    driver = JobDriver(db_session)
    await driver.fail_job(UUID(job_id_1))

    # Re-submit same video
    response2 = await client.post(
        "/api/search",
        json={"youtube_url": video.video_url, "keyword": "test"},
    )
    assert response2.status_code == 202
    job_id_2 = response2.json()["job_id"]

    # Should be a different job
    assert job_id_1 != job_id_2


@pytest.mark.asyncio
async def test_concurrent_dedupe_race_safe(
    client_per_request: AsyncClient,
) -> None:
    """
    Test concurrent POSTs for same video create exactly one job (NN-8).
    Uses real concurrent requests to verify the DB unique constraint.
    """
    import asyncio

    from tests.conftest import TestAsyncSessionFactory

    video_url = "https://www.youtube.com/watch?v=concrtest12"

    async def make_request():
        return await client_per_request.post(
            "/api/search",
            json={"youtube_url": video_url, "keyword": "test"},
        )

    # Fire 10 concurrent requests
    responses = await asyncio.gather(*[make_request() for _ in range(10)])

    # All should succeed
    for resp in responses:
        assert resp.status_code in (200, 202)

    # All should return the same job_id
    job_ids = [resp.json()["job_id"] for resp in responses]
    assert len(set(job_ids)) == 1, f"Expected 1 unique job_id, got {set(job_ids)}"

    # Verify only one video and one job in database
    async with TestAsyncSessionFactory() as session:
        from sqlalchemy import func, select

        videos = await session.execute(select(Video).where(Video.video_url == video_url))
        assert len(videos.scalars().all()) == 1

        job_count = await session.execute(
            select(func.count(Job.id))
            .join(Video, Job.video_id == Video.id)
            .where(Video.video_url == video_url)
        )
        assert job_count.scalar() == 1


@pytest.mark.asyncio
async def test_post_completion_research_is_cache_hit(
    client: AsyncClient,
    db_session,
) -> None:
    """
    Test that after job completion, re-search is a cache hit (no new job).
    """
    video = Video(
        video_url="https://www.youtube.com/watch?v=cachehit123",
        video_id="cachehit123",
        title="Cache Hit Test",
    )
    db_session.add(video)
    await db_session.flush()

    # First search
    response1 = await client.post(
        "/api/search",
        json={"youtube_url": video.video_url, "keyword": "hello world"},
    )
    assert response1.status_code == 202
    job_id = response1.json()["job_id"]

    # Complete the job
    driver = JobDriver(db_session)
    await driver.complete_with_fake_transcript(UUID(job_id))

    # Re-search same video + keyword
    response2 = await client.post(
        "/api/search",
        json={"youtube_url": video.video_url, "keyword": "hello world"},
    )

    # Should be cache hit (200), not 202
    assert response2.status_code == 200
    assert response2.json()["status"] == "found"
    assert len(response2.json()["results"]) == 1
