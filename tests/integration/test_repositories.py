"""Repository tests covering core CRUD and edge branches."""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, Video
from app.repositories import JobRepository, VideoRepository


async def _make_video(db_session: AsyncSession, *, suffix: str = "") -> Video:
    """Create a video row for repository tests."""
    uid = uuid4().hex[:12]
    video = Video(
        video_url=f"https://www.youtube.com/watch?v={uid}{suffix}",
        video_id=f"{uid}{suffix}",
        language="en",
    )
    db_session.add(video)
    await db_session.flush()
    await db_session.refresh(video)
    return video


class TestVideoRepository:
    """Tests for VideoRepository."""

    @pytest.mark.asyncio
    async def test_get_by_id(self, db_session: AsyncSession) -> None:
        video = await _make_video(db_session)

        repo = VideoRepository(db_session)

        assert (await repo.get_by_id(video.id)).id == video.id
        assert await repo.get_by_id(uuid4()) is None

    @pytest.mark.asyncio
    async def test_get_by_video_id(self, db_session: AsyncSession) -> None:
        video = await _make_video(db_session)

        repo = VideoRepository(db_session)

        assert (await repo.get_by_video_id(video.video_id)).id == video.id
        assert await repo.get_by_video_id("missing") is None

    @pytest.mark.asyncio
    async def test_create(self, db_session: AsyncSession) -> None:
        repo = VideoRepository(db_session)

        video = await repo.create(
            video_url="https://www.youtube.com/watch?v=newid111111",
            video_id="newid111111",
            language="en",
            title="A Title",
            duration_seconds=100,
        )

        assert video.video_id == "newid111111"
        assert video.title == "A Title"
        assert video.duration_seconds == 100

    @pytest.mark.asyncio
    async def test_update_metadata(self, db_session: AsyncSession) -> None:
        video = await _make_video(db_session)
        repo = VideoRepository(db_session)

        result = await repo.update_metadata(video.id, title="New Title", duration_seconds=500)

        assert result.title == "New Title"
        assert result.duration_seconds == 500

        with pytest.raises(ValueError):
            await repo.update_metadata(uuid4(), title="X", duration_seconds=1)

    @pytest.mark.asyncio
    async def test_update_transcript(self, db_session: AsyncSession) -> None:
        video = await _make_video(db_session)
        repo = VideoRepository(db_session)

        result = await repo.update_transcript(
            video.id, transcript="hello world", language="ar", provider="vidwords"
        )
        await db_session.flush()
        await db_session.refresh(result)

        assert result.transcript == "hello world"
        assert result.language == "ar"
        assert result.provider == "vidwords"
        assert result.transcribed_at is not None

        with pytest.raises(ValueError):
            await repo.update_transcript(uuid4(), transcript="x")

    @pytest.mark.asyncio
    async def test_is_transcribed(self, db_session: AsyncSession) -> None:
        video = await _make_video(db_session)
        repo = VideoRepository(db_session)

        assert await repo.is_transcribed(video.id) is False

        await repo.update_transcript(video.id, transcript="hi")
        assert await repo.is_transcribed(video.id) is True


class TestJobRepository:
    """Tests for JobRepository."""

    @pytest.mark.asyncio
    async def test_get_by_id(self, db_session: AsyncSession) -> None:
        video = await _make_video(db_session)
        job = Job(video_id=video.id, status="pending")
        db_session.add(job)
        await db_session.flush()

        repo = JobRepository(db_session)

        assert (await repo.get_by_id(job.id)).id == job.id
        assert await repo.get_by_id(uuid4()) is None

    @pytest.mark.asyncio
    async def test_create_or_get_in_flight_new(self, db_session: AsyncSession) -> None:
        video = await _make_video(db_session)
        repo = JobRepository(db_session)

        job = await repo.create_or_get_in_flight(video.id)

        assert job.video_id == video.id
        assert job.status == "pending"

        # Calling again returns the existing in-flight job
        again = await repo.create_or_get_in_flight(video.id)
        assert again.id == job.id

    @pytest.mark.asyncio
    async def test_create_or_get_in_flight_reuses_processing(
        self, db_session: AsyncSession
    ) -> None:
        video = await _make_video(db_session)
        repo = JobRepository(db_session)
        job = Job(video_id=video.id, status="processing")
        db_session.add(job)
        await db_session.flush()

        result = await repo.create_or_get_in_flight(video.id)

        assert result.id == job.id
        assert result.status == "processing"

    @pytest.mark.asyncio
    async def test_update_status(self, db_session: AsyncSession) -> None:
        video = await _make_video(db_session)
        job = Job(video_id=video.id, status="pending")
        db_session.add(job)
        await db_session.flush()

        repo = JobRepository(db_session)

        updated = await repo.update_status(job.id, status="processing", progress=10, error=None)
        assert updated.status == "processing"
        assert updated.progress == 10

        with pytest.raises(ValueError):
            await repo.update_status(uuid4(), status="completed")
