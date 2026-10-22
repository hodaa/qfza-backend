"""Integration tests for the transcript PDF download endpoint."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TranscriptWord, Video


def _video(video_id: str, *, transcribed: bool = True) -> Video:
    """Build a video row, transcribed unless the test says otherwise."""
    return Video(
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
        title="Test Video",
        duration_seconds=100,
        language="en",
        transcribed_at=datetime.now(UTC) if transcribed else None,
    )


async def _add_words(
    db_session: AsyncSession,
    video: Video,
    words: list[tuple[str, float, float]],
) -> None:
    """Append transcript words to a video, in transcript order."""
    for index, (text, start, end) in enumerate(words):
        db_session.add(
            TranscriptWord(
                video_id=video.id,
                word_index=index,
                word=text,
                start_time=start,
                end_time=end,
            )
        )
    await db_session.flush()


@pytest.mark.asyncio
async def test_unknown_video_returns_404(client: AsyncClient) -> None:
    response = await client.get("/api/videos/nosuchvideo/transcript.pdf")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_untranscribed_video_returns_404(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    db_session.add(_video("pending1", transcribed=False))
    await db_session.flush()

    response = await client.get("/api/videos/pending1/transcript.pdf")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "VIDEO_NOT_TRANSCRIBED"


@pytest.mark.asyncio
async def test_transcribed_video_without_words_returns_404(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    db_session.add(_video("empty1"))
    await db_session.flush()

    response = await client.get("/api/videos/empty1/transcript.pdf")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "VIDEO_NOT_TRANSCRIBED"


@pytest.mark.asyncio
async def test_returns_pdf_attachment_for_transcribed_video(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    video = _video("pdfvideo1")
    db_session.add(video)
    await db_session.flush()
    await _add_words(
        db_session,
        video,
        [
            ("buy", 12.0, 12.4),
            ("a", 12.4, 12.5),
            ("brain.", 12.5, 13.0),
        ],
    )

    response = await client.get("/api/videos/pdfvideo1/transcript.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["content-disposition"] == 'attachment; filename="transcript-pdfvideo1.pdf"'
    )
    assert response.content.startswith(b"%PDF")
    assert len(response.content) > 1000


@pytest.mark.asyncio
async def test_pdf_embeds_font_for_arabic_transcript(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    video = _video("arabic1")
    db_session.add(video)
    await db_session.flush()
    await _add_words(
        db_session,
        video,
        [
            ("مرحبا", 0.0, 0.5),
            ("بكم", 0.5, 0.9),
        ],
    )

    response = await client.get("/api/videos/arabic1/transcript.pdf")

    assert response.status_code == 200
    assert b"FontFile2" in response.content
