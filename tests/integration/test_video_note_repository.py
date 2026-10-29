"""Integration tests for the lecture-notes repository."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Video, VideoNote
from app.repositories.video_note_repository import VideoNoteRepository


async def _video(db_session: AsyncSession, video_id: str = "notesrepo1") -> Video:
    """Store a video that notes can belong to."""
    video = Video(
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
        title="Repository Lecture",
        transcribed_at=datetime.now(UTC),
    )
    db_session.add(video)
    await db_session.flush()
    return video


def _note(video_id, *, status: str = "pending", age_seconds: int = 0) -> VideoNote:
    """Build a note row with a deterministic age."""
    note = VideoNote(video_id=video_id, status=status)
    note.updated_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return note


class TestVideoNoteRepository:
    """The ledger's queries and status writes."""

    @pytest.mark.asyncio
    async def test_create_and_lookups(self, db_session: AsyncSession) -> None:
        video = await _video(db_session)
        repo = VideoNoteRepository(db_session)

        created = await repo.create(video_id=video.id)

        assert created.status == "pending"
        assert await repo.get_by_id(created.id) is created
        assert await repo.get_for_video(created.id, video.id) is created
        assert await repo.get_active(video.id) is created
        assert await repo.get_for_video(created.id, uuid4()) is None

    @pytest.mark.asyncio
    async def test_ready_lookup_returns_the_newest(self, db_session: AsyncSession) -> None:
        video = await _video(db_session, "notesrepo2")
        repo = VideoNoteRepository(db_session)
        older = _note(video.id, status="ready", age_seconds=60)
        newer = _note(video.id, status="ready", age_seconds=1)
        older.created_at = datetime.now(UTC) - timedelta(minutes=2)
        newer.created_at = datetime.now(UTC) - timedelta(minutes=1)
        db_session.add_all([older, newer])
        await db_session.flush()

        assert await repo.get_ready(video.id) is newer

    @pytest.mark.asyncio
    async def test_active_excludes_finished_rows(self, db_session: AsyncSession) -> None:
        video = await _video(db_session, "notesrepo3")
        repo = VideoNoteRepository(db_session)
        db_session.add(_note(video.id, status="ready"))
        await db_session.flush()

        assert await repo.get_active(video.id) is None

    @pytest.mark.asyncio
    async def test_update_status_sets_fields(self, db_session: AsyncSession) -> None:
        video = await _video(db_session, "notesrepo4")
        repo = VideoNoteRepository(db_session)
        note = await repo.create(video_id=video.id)

        updated = await repo.update_status(
            note.id, status="ready", blob_url="https://blob/notes.pdf"
        )

        assert updated.status == "ready"
        assert updated.blob_url == "https://blob/notes.pdf"

    @pytest.mark.asyncio
    async def test_update_status_rejects_an_unknown_row(self, db_session: AsyncSession) -> None:
        repo = VideoNoteRepository(db_session)

        with pytest.raises(ValueError):
            await repo.update_status(uuid4(), status="ready")

    @pytest.mark.asyncio
    async def test_lists_only_stale_active_rows(self, db_session: AsyncSession) -> None:
        video = await _video(db_session, "notesrepo5")
        fresh_video = await _video(db_session, "notesrepo6")
        finished_video = await _video(db_session, "notesrepo7")
        repo = VideoNoteRepository(db_session)
        stale = _note(video.id, status="processing", age_seconds=7200)
        fresh = _note(fresh_video.id, status="pending", age_seconds=5)
        finished = _note(finished_video.id, status="failed", age_seconds=7200)
        db_session.add_all([stale, fresh, finished])
        await db_session.flush()

        rows = await repo.list_stale_active(datetime.now(UTC) - timedelta(hours=1), 10)

        assert rows == [stale]
