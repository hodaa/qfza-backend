"""API routes for the JumpTo application."""

import asyncio
import re
import shutil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.api.identity import resolve_identity
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.exceptions import ValidationError, VideoNotFoundError, VideoNotTranscribedError
from app.core.logging import get_logger
from app.models import Video
from app.repositories import (
    ContactRepository,
    JobRepository,
    TranscriptWordRepository,
    VideoRepository,
)
from app.repositories.search_history_repository import SearchHistoryRepository
from app.schemas import (
    ContactRequest,
    ContactResponse,
    FullTextSearchResponse,
    SearchRequest,
    SearchResponse,
    SearchResponseCached,
    SearchResponseProcessing,
    SearchStatus,
    StatusResponse,
    VideoFormatsResponse,
    VideoSearchResponse,
)
from app.services import (
    MAX_AVC1_HEIGHT,
    MIN_VIDEO_HEIGHT,
    FullTextSearchService,
    JobService,
    SearchService,
    TranscriptPdfRenderer,
    assemble_lines,
    download_video_file,
    is_rtl_transcript,
    list_video_heights,
    validate_youtube_url,
)
from app.services.history_recorder import (
    STATUS_FOUND,
    STATUS_NOT_FOUND,
    STATUS_PROCESSING,
    SearchHistoryRecorder,
)
from app.services.messaging import dispatch_transcription

router = APIRouter()

logger = get_logger(__name__)


def get_video_repo(session: AsyncSession = Depends(get_db_session)) -> VideoRepository:
    """Build a video repository for the request session."""
    return VideoRepository(session)


def get_transcript_repo(
    session: AsyncSession = Depends(get_db_session),
) -> TranscriptWordRepository:
    """Build a transcript repository for the request session."""
    return TranscriptWordRepository(session)


def get_job_repo(session: AsyncSession = Depends(get_db_session)) -> JobRepository:
    """Build a job repository for the request session."""
    return JobRepository(session)


def get_contact_repo(session: AsyncSession = Depends(get_db_session)) -> ContactRepository:
    """Build a contact repository for the request session."""
    return ContactRepository(session)


def get_search_service(
    transcript_repo: TranscriptWordRepository = Depends(get_transcript_repo),
) -> SearchService:
    """Build the search service."""
    return SearchService(transcript_repo)


def get_fulltext_service(
    video_repo: VideoRepository = Depends(get_video_repo),
) -> FullTextSearchService:
    """Build the full-text catalog search service."""
    return FullTextSearchService(video_repo)


def get_job_service(
    job_repo: JobRepository = Depends(get_job_repo),
    video_repo: VideoRepository = Depends(get_video_repo),
) -> JobService:
    """Build the job service."""
    return JobService(job_repo, video_repo)


def get_history_recorder(
    session: AsyncSession = Depends(get_db_session),
) -> SearchHistoryRecorder:
    """Build the recorder that files a search under the caller's account."""
    return SearchHistoryRecorder(SearchHistoryRepository(session), session)


@router.post(
    "/api/search",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"model": SearchResponseCached, "description": "Cached results found"},
        202: {"model": SearchResponseProcessing, "description": "Job created, processing started"},
        400: {"description": "Invalid YouTube URL"},
        422: {"description": "Validation error"},
    },
)
async def search(
    request: SearchRequest,
    request_object: Request,
    video_repo: VideoRepository = Depends(get_video_repo),
    search_service: SearchService = Depends(get_search_service),
    job_service: JobService = Depends(get_job_service),
    recorder: SearchHistoryRecorder = Depends(get_history_recorder),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> SearchResponse:
    """
    Search for a keyword in a YouTube video transcript (cache-first).

    Returns cached results immediately when the video is already transcribed;
    otherwise queues a transcription job and returns a job id.

    Anonymous and signed-in visitors take exactly the same path. When a session
    is present the search is also filed under that account, which is why the
    identity lookup here is optional and swallows its own failures.
    """
    caller_id = await _optional_caller_id(request_object, settings, session)

    async def remember(
        status: str,
        progress_seconds: int | None = None,
        match_timestamps: list[int] | None = None,
        match_results: list[dict[str, object]] | None = None,
    ) -> None:
        # The title is snapshotted here, while the row is in hand. The history
        # page then keeps calling the video whatever it was called at the moment
        # of the search, even if the transcript is refetched and the name changes.
        await recorder.record(
            user_id=caller_id,
            video_id=youtube_info.video_id,
            keyword=request.keyword,
            status=status,
            locale=_client_locale(request_object),
            source=_client_source(request_object),
            progress_seconds=progress_seconds,
            video_title=getattr(video, "title", None) if video is not None else None,
            match_timestamps=match_timestamps,
            match_results=match_results,
        )

    youtube_info = validate_youtube_url(str(request.youtube_url))
    video = await video_repo.get_by_video_id_lite(youtube_info.video_id)

    if video and video.transcribed_at:
        results = await search_service.search(video.id, request.keyword)
        # A transcribed video with a null transcript has no speech/sound at all;
        # surface that distinctly from "keyword not found in a real transcript".
        no_speech = not results and not await video_repo.has_speech_text(video.id)
        if not results:
            await remember(STATUS_NOT_FOUND)
            return SearchResponseCached(
                status=SearchStatus.NOT_FOUND, results=[], no_speech=no_speech
            )
        # Where the first match sat, so reopening this entry from the history
        # page resumes at the moment the visitor was reading instead of at 00:00.
        # Rounded to whole seconds: that is the resolution the player honours
        # anyway, and a fractional column would imply a precision it lacks.
        await remember(
            STATUS_FOUND,
            progress_seconds=int(results[0].progress_seconds),
            match_timestamps=[int(r.progress_seconds) for r in results],
            # The whole result set, snippet included: reopening this entry from
            # history should show the same list the search produced, not just
            # where to seek. Stored as plain JSON via model_dump so the shape is
            # exactly what the client already consumes from this endpoint.
            match_results=[r.model_dump(mode="json") for r in results],
        )
        return SearchResponseCached(status="found", results=results)

    if not video:
        video = await _get_or_create_video(
            video_repo,
            youtube_info.original_url,
            youtube_info.video_id,
        )

    job = await job_service.create_or_get_job(video.id)
    # Recorded before the commit so the row lands in the same transaction as the
    # video and job it refers to.
    await remember(STATUS_PROCESSING)
    await session.commit()
    await asyncio.to_thread(_dispatch_pipeline, job.id)
    response = SearchResponseProcessing(status="processing", job_id=job.id, video_id=video.id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=response.model_dump(mode="json"),
    )


async def _optional_caller_id(
    request: Request, settings: Settings, db: AsyncSession
) -> UUID | None:
    """Return the signed-in user id, or None for an anonymous visitor.

    Never raises. A bad cookie, an expired session, or the session table being
    unreachable all mean "nobody", because a public search must not depend on
    the account system being healthy.
    """
    token = request.cookies.get(settings.session_cookie_name) or _bearer_token(
        request.headers.get("Authorization")
    )
    if not token:
        return None
    try:
        identity = await resolve_identity(db=db, settings=settings, token=token)
    except Exception:
        logger.warning("Could not resolve an optional session on /api/search", exc_info=True)
        return None
    return identity.user.id if identity else None


def _bearer_token(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _client_locale(request: Request) -> str | None:
    """Best-effort UI language from Accept-Language, stored for later filtering."""
    header = request.headers.get("Accept-Language")
    if not header:
        return None
    top = header.split(",")[0].strip().lower()
    return top[:10] or None


def _client_source(request: Request) -> str | None:
    return request.headers.get("X-Qfza-Source")


@router.post(
    "/api/contact",
    response_model=ContactResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"model": ContactResponse, "description": "Contact message stored"},
        422: {"description": "Validation error"},
    },
)
async def create_contact_message(
    request: ContactRequest,
    contact_repo: ContactRepository = Depends(get_contact_repo),
    session: AsyncSession = Depends(get_db_session),
) -> ContactResponse:
    """Store a contact form message from the public website."""
    record = await contact_repo.create(
        name=request.name,
        email=request.email,
        message=request.message,
    )
    await session.commit()
    return ContactResponse(id=record.id, created_at=record.created_at)


@router.get(
    "/api/status/{job_id}",
    response_model=StatusResponse,
    responses={
        200: {"model": StatusResponse, "description": "Job status retrieved"},
        404: {"description": "Job not found"},
    },
)
async def get_job_status(
    job_id: UUID,
    job_service: JobService = Depends(get_job_service),
) -> StatusResponse:
    """Return the current status of a transcription job."""
    job = await job_service.get_job(job_id)
    return StatusResponse(
        status=job.status,
        video_id=job.video_id,
        progress=job.progress,
        results=None,
        error=job.error,
    )


@router.get(
    "/api/video/{video_id}/search",
    response_model=VideoSearchResponse,
    responses={
        200: {"model": VideoSearchResponse, "description": "Search results"},
        400: {"description": "Missing or invalid keyword"},
        404: {"description": "Video not found or not transcribed"},
    },
)
async def search_video(
    video_id: UUID,
    keyword: str = Query(..., description="Keyword or phrase to search"),
    video_repo: VideoRepository = Depends(get_video_repo),
    search_service: SearchService = Depends(get_search_service),
) -> VideoSearchResponse:
    """Search for a keyword within a specific transcribed video."""
    if not keyword.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keyword cannot be empty or whitespace only",
        )

    video = await video_repo.get_by_id_lite(video_id)
    if not video:
        raise VideoNotFoundError(str(video_id))

    if not video.transcribed_at:
        raise VideoNotTranscribedError(str(video_id))

    results = await search_service.search(video_id, keyword.strip())
    if not results:
        no_speech = not await video_repo.has_speech_text(video_id)
        return VideoSearchResponse(status=SearchStatus.NOT_FOUND, results=[], no_speech=no_speech)
    return VideoSearchResponse(status="found", results=results)


def _download_filename(
    youtube_video_id: str,
    *,
    prefix: str,
    suffix: str,
    range_start: int | None = None,
    range_end: int | None = None,
    height: int | None = None,
) -> str:
    """Build a download filename that is safe to place in a response header."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", youtube_video_id)[:64] or "video"
    label = f"-{height}p" if height is not None else ""
    if range_start is not None:
        label += f"-{range_start}-{range_end if range_end is not None else 'end'}"
    return f"{prefix}-{safe_id}{label}{suffix}"


def _validated_range(
    start: float | None, end: float | None, duration_seconds: int | None
) -> tuple[int | None, int | None]:
    """Validate the requested download range against the video's duration.

    Fractional seconds are truncated to whole seconds, since match timestamps
    arrive as floats while both YouTube moments and yt-dlp sections are
    whole-second in practice. An end beyond the video is clamped rather than
    refused, so a caller that only knows roughly where the interesting part
    stops still gets a file.
    """
    if start is None:
        if end is not None:
            raise ValidationError("An end time requires a start time", field="end")
        return None, None
    whole_start = int(start)
    whole_end = int(end) if end is not None else None
    if whole_end is not None and whole_end <= whole_start:
        raise ValidationError("The end time must be after the start time", field="end")
    if duration_seconds is not None:
        if whole_start >= duration_seconds:
            raise ValidationError("The start time is beyond the video's duration", field="start")
        if whole_end is not None and whole_end > duration_seconds:
            whole_end = duration_seconds
    return whole_start, whole_end


@router.get(
    "/api/videos/{youtube_video_id}/transcript.pdf",
    responses={
        200: {"content": {"application/pdf": {}}, "description": "Transcript PDF"},
        404: {"description": "Video not found or not transcribed"},
    },
)
async def download_transcript_pdf(
    youtube_video_id: str,
    video_repo: VideoRepository = Depends(get_video_repo),
    transcript_repo: TranscriptWordRepository = Depends(get_transcript_repo),
) -> Response:
    """Download a transcribed video's full transcript as a PDF document."""
    video = await video_repo.get_by_video_id_lite(youtube_video_id)
    if not video:
        raise VideoNotFoundError(youtube_video_id)
    if not video.transcribed_at:
        raise VideoNotTranscribedError(youtube_video_id)

    words = await transcript_repo.list_all_for_video(video.id)
    if not words:
        raise VideoNotTranscribedError(youtube_video_id)

    document = TranscriptPdfRenderer().render(
        title=video.title or youtube_video_id,
        lines=assemble_lines(words),
        is_rtl=is_rtl_transcript(words, video.language),
    )
    return Response(
        content=document,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                "attachment; filename="
                f'"{_download_filename(youtube_video_id, prefix="transcript", suffix=".pdf")}"'
            )
        },
    )


@router.get(
    "/api/videos/{youtube_video_id}/video.mp4",
    responses={
        200: {"content": {"video/mp4": {}}, "description": "Video file"},
        404: {"description": "Video not found"},
        422: {"description": "Validation error"},
        502: {"description": "YouTube could not provide the video"},
        503: {"description": "Download tooling is unavailable in this deployment"},
    },
)
async def download_video(
    youtube_video_id: str,
    start: float | None = Query(None, ge=0, description="Section start in seconds"),
    end: float | None = Query(None, ge=0, description="Section end in seconds"),
    height: int | None = Query(
        None,
        ge=MIN_VIDEO_HEIGHT,
        le=MAX_AVC1_HEIGHT,
        description="Preferred H.264 quality in pixels",
    ),
    video_repo: VideoRepository = Depends(get_video_repo),
) -> FileResponse:
    """Download a video's media as an MP4 file, or one section of it.

    The fetch happens while the request is open: yt-dlp downloads and merges
    the streams before the first byte leaves, so the browser waits and then
    receives a complete file with a known length. With ``start``, only the
    section from it to ``end`` (or the video's end) is kept. With ``height``,
    the file is capped at that H.264 quality.
    """
    video = await video_repo.get_by_video_id_lite(youtube_video_id)
    if not video:
        raise VideoNotFoundError(youtube_video_id)

    start, end = _validated_range(start, end, video.duration_seconds)
    path = await download_video_file(
        youtube_video_id, start_seconds=start, end_seconds=end, height=height
    )
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=_download_filename(
            youtube_video_id,
            prefix="video",
            suffix=".mp4",
            range_start=start,
            range_end=end,
            height=height,
        ),
        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True),
    )


@router.get(
    "/api/videos/{youtube_video_id}/formats",
    response_model=VideoFormatsResponse,
    responses={
        404: {"description": "Video not found"},
        502: {"description": "YouTube could not provide the video's formats"},
        503: {"description": "Download tooling is unavailable in this deployment"},
    },
)
async def list_video_formats(
    youtube_video_id: str,
    video_repo: VideoRepository = Depends(get_video_repo),
) -> VideoFormatsResponse:
    """List the heights a video can be downloaded in, tallest first.

    A metadata-only probe on YouTube; an empty list means no H.264 quality is
    named and the download should fall back to its best available stream.
    """
    video = await video_repo.get_by_video_id_lite(youtube_video_id)
    if not video:
        raise VideoNotFoundError(youtube_video_id)
    return VideoFormatsResponse(heights=await list_video_heights(youtube_video_id))


@router.get(
    "/api/videos/search",
    response_model=FullTextSearchResponse,
    responses={422: {"description": "Validation error"}},
)
async def search_videos_by_text(
    q: str = Query(..., min_length=1, max_length=200, description="Full-text query"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    fulltext_service: FullTextSearchService = Depends(get_fulltext_service),
) -> FullTextSearchResponse:
    """Search transcribed videos by transcript content via PostgreSQL FTS.

    Query semantics follow ``websearch_to_tsquery``: plain words AND together,
    an explicit ``OR`` unions terms, double-quoted phrases match exactly,
    and stemming is applied.
    """
    results = await fulltext_service.search(q, limit=limit)
    status = SearchStatus.FOUND if results else SearchStatus.NOT_FOUND
    return FullTextSearchResponse(status=status, query=q.strip(), results=results)


async def _get_or_create_video(
    video_repo: VideoRepository,
    youtube_url: str,
    youtube_id: str,
    language: str = "en",
) -> Video:
    """Create a video record, resolving a concurrent-creation race."""
    try:
        return await video_repo.create(
            youtube_url=youtube_url, video_id=youtube_id, language=language
        )
    except IntegrityError:
        await video_repo.session.rollback()
        video = await video_repo.get_by_video_id_lite(youtube_id)
        if video:
            return video
        raise


def _dispatch_pipeline(job_id: UUID) -> None:
    """Publish the transcription job to the standalone worker."""
    dispatch_transcription(job_id)
