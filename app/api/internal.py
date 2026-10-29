"""Internal API routes for worker-to-backend communication."""

import math
import os
import platform
import statistics
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.exceptions import VideoNotFoundError
from app.models import TranscriptWord
from app.repositories import (
    JobRepository,
    TranscriptWordRepository,
    VideoDownloadRepository,
    VideoNoteRepository,
    VideoRepository,
)
from app.schemas import (
    BenchmarkBackendInfo,
    BenchmarkDatabaseInfo,
    BenchmarkLatency,
    BenchmarkResponse,
    InternalCompleteRequest,
    InternalDownloadFailRequest,
    InternalDownloadReadyRequest,
    InternalDownloadResponse,
    InternalFailRequest,
    InternalJobResponse,
    InternalNotesResponse,
    InternalProgressRequest,
    InternalStatusResponse,
    InternalStoreTranscriptRequest,
    InternalVideoFormatsRequest,
)
from app.services import (
    JobService,
    VideoDownloadService,
    VideoNotesService,
    assemble_lines,
    is_rtl_transcript,
)

router = APIRouter()


async def _verify_internal_api_key(
    x_internal_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """Verify the caller presents the configured internal API key."""
    expected = get_settings().internal_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API is not configured",
        )
    if x_internal_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal API key",
        )


@router.get(
    "/internal/benchmark/db",
    response_model=BenchmarkResponse,
    dependencies=[Depends(_verify_internal_api_key)],
    summary="Measure backend-to-database latency",
)
async def benchmark_database(
    response: Response,
    samples: Annotated[int, Query(ge=1, le=10)] = 5,
    session: AsyncSession = Depends(get_db_session),
) -> BenchmarkResponse:
    """Measure cold connection time and warm database round trips.

    The first query includes connection setup (DNS, TCP, TLS, auth), which is
    the cost paid by a fresh serverless invocation. Subsequent ``SELECT 1``
    queries run on the same connection and approximate pure network + database
    round-trip time.
    """
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["CDN-Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"

    backend_timestamp = datetime.now(UTC)
    settings = get_settings()
    parsed_url = urlparse(settings.database_url)

    connect_started = perf_counter()
    await session.connection()
    connect_ms = (perf_counter() - connect_started) * 1000

    query_started = perf_counter()
    result = await session.execute(text("SELECT now(), version()"))
    first_query_ms = (perf_counter() - query_started) * 1000
    db_timestamp, server_version = result.one()

    warm_samples: list[float] = []
    for _ in range(samples):
        query_started = perf_counter()
        await session.execute(text("SELECT 1"))
        warm_samples.append((perf_counter() - query_started) * 1000)

    ordered = sorted(warm_samples)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)

    return BenchmarkResponse(
        backend=BenchmarkBackendInfo(
            timestamp=backend_timestamp,
            region=os.getenv("VERCEL_REGION") or os.getenv("AWS_REGION") or os.getenv("OCI_REGION"),
            platform="vercel" if os.getenv("VERCEL") else platform.system().lower(),
        ),
        database=BenchmarkDatabaseInfo(
            timestamp=db_timestamp,
            server_version=server_version,
            host=parsed_url.hostname,
            name=parsed_url.path.lstrip("/") or None,
        ),
        latency=BenchmarkLatency(
            connect_ms=round(connect_ms, 2),
            first_query_ms=round(first_query_ms, 2),
            min_ms=round(ordered[0], 2),
            median_ms=round(statistics.median(ordered), 2),
            mean_ms=round(statistics.fmean(ordered), 2),
            p95_ms=round(ordered[p95_index], 2),
            samples_ms=[round(sample, 2) for sample in warm_samples],
        ),
        samples=samples,
    )


async def _get_job_service(session: AsyncSession = Depends(get_db_session)) -> JobService:
    """Build a job service bound to the request session."""
    return JobService(JobRepository(session), VideoRepository(session))


async def _get_video_repo(session: AsyncSession = Depends(get_db_session)) -> VideoRepository:
    """Build a video repository bound to the request session."""
    return VideoRepository(session)


async def _get_word_repo(
    session: AsyncSession = Depends(get_db_session),
) -> TranscriptWordRepository:
    """Build a transcript-word repository bound to the request session."""
    return TranscriptWordRepository(session)


async def _get_download_service(
    session: AsyncSession = Depends(get_db_session),
) -> VideoDownloadService:
    """Build a download service bound to the request session."""
    return VideoDownloadService(VideoRepository(session), VideoDownloadRepository(session))


async def _get_notes_service(
    session: AsyncSession = Depends(get_db_session),
) -> VideoNotesService:
    """Build a lecture-notes service bound to the request session."""
    return VideoNotesService(VideoRepository(session), VideoNoteRepository(session))


@router.get(
    "/internal/jobs/{job_id}",
    response_model=InternalJobResponse,
    dependencies=[Depends(_verify_internal_api_key)],
    responses={401: {"description": "Invalid API key"}, 404: {"description": "Job not found"}},
)
async def internal_get_job(
    job_id: UUID,
    job_service: JobService = Depends(_get_job_service),
    video_repo: VideoRepository = Depends(_get_video_repo),
) -> InternalJobResponse:
    """Return job and video data needed by the worker to run the pipeline."""
    job = await job_service.get_job(job_id)
    video = await video_repo.get_by_id_lite(job.video_id)
    if not video:
        raise VideoNotFoundError(str(job.video_id))
    return InternalJobResponse(
        job_id=job.id,
        video_id=job.video_id,
        youtube_video_id=video.video_id,
        youtube_url=video.video_url,
        status=job.status,
    )


@router.post(
    "/internal/jobs/{job_id}/advance",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_advance_job(
    job_id: UUID,
    job_service: JobService = Depends(_get_job_service),
) -> InternalStatusResponse:
    """Mark a job as processing."""
    await job_service.get_job(job_id)
    job = await job_service.advance_to_processing(job_id)
    return InternalStatusResponse(status=job.status)


@router.post(
    "/internal/jobs/{job_id}/progress",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_update_progress(
    job_id: UUID,
    request: InternalProgressRequest,
    job_service: JobService = Depends(_get_job_service),
) -> InternalStatusResponse:
    """Update intermediate progress for a job reported by the worker."""
    await job_service.get_job(job_id)
    job = await job_service.set_progress(job_id, request.progress)
    return InternalStatusResponse(status=job.status)


@router.post(
    "/internal/jobs/{job_id}/transcript",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_store_transcript(
    job_id: UUID,
    request: InternalStoreTranscriptRequest,
    job_service: JobService = Depends(_get_job_service),
    video_repo: VideoRepository = Depends(_get_video_repo),
    word_repo: TranscriptWordRepository = Depends(_get_word_repo),
    session: AsyncSession = Depends(get_db_session),
) -> InternalStatusResponse:
    """Store transcript words and update video metadata + full-text index."""
    job = await job_service.get_job(job_id)
    video = await video_repo.get_by_id(job.video_id)
    if not video:
        raise VideoNotFoundError(str(job.video_id))

    await word_repo.delete_by_video_id(video.id)
    words = [
        TranscriptWord(
            video_id=video.id,
            word_index=item.word_index,
            word=item.word,
            start_time=item.start_time,
            end_time=item.end_time,
        )
        for item in request.words
    ]
    if words:
        await word_repo.bulk_create(words)
    await video_repo.update_metadata(
        video.id,
        title=request.title,
        duration_seconds=request.duration_seconds,
    )
    await video_repo.update_transcript(
        video.id,
        transcript=request.transcript_text,
        language=request.language,
        provider=request.provider,
    )
    await session.commit()
    return InternalStatusResponse(status=job.status)


@router.post(
    "/internal/jobs/{job_id}/complete",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_complete_job(
    job_id: UUID,
    request: InternalCompleteRequest | None = None,
    job_service: JobService = Depends(_get_job_service),
    session: AsyncSession = Depends(get_db_session),
) -> InternalStatusResponse:
    """Mark a job as completed, optionally carrying an outcome note."""
    await job_service.get_job(job_id)
    job = await job_service.complete_job(
        job_id,
        error=request.message if request else None,
    )
    await session.commit()
    return InternalStatusResponse(status=job.status)


@router.post(
    "/internal/jobs/{job_id}/fail",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_fail_job(
    job_id: UUID,
    request: InternalFailRequest,
    job_service: JobService = Depends(_get_job_service),
    session: AsyncSession = Depends(get_db_session),
) -> InternalStatusResponse:
    """Mark a job as failed with a user-safe error message."""
    await job_service.get_job(job_id)
    job = await job_service.fail_job(job_id, request.error)
    await session.commit()
    return InternalStatusResponse(status=job.status)


@router.get(
    "/internal/downloads/{download_id}",
    response_model=InternalDownloadResponse,
    dependencies=[Depends(_verify_internal_api_key)],
    responses={401: {"description": "Invalid API key"}, 404: {"description": "Download not found"}},
)
async def internal_get_download(
    download_id: UUID,
    download_service: VideoDownloadService = Depends(_get_download_service),
) -> InternalDownloadResponse:
    """Return download and video data needed by the worker to produce a file."""
    download, video = await download_service.get_for_worker(download_id)
    return InternalDownloadResponse(
        download_id=download.id,
        video_id=download.video_id,
        youtube_video_id=video.video_id,
        youtube_url=video.video_url,
        status=download.status,
        start_seconds=download.start_seconds,
        end_seconds=download.end_seconds,
        height=download.height,
    )


@router.post(
    "/internal/downloads/{download_id}/advance",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_advance_download(
    download_id: UUID,
    download_service: VideoDownloadService = Depends(_get_download_service),
) -> InternalStatusResponse:
    """Mark a download as processing."""
    await download_service.get_by_id(download_id)
    download = await download_service.mark_processing(download_id)
    return InternalStatusResponse(status=download.status)


@router.post(
    "/internal/downloads/{download_id}/ready",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_mark_download_ready(
    download_id: UUID,
    request: InternalDownloadReadyRequest,
    download_service: VideoDownloadService = Depends(_get_download_service),
    session: AsyncSession = Depends(get_db_session),
) -> InternalStatusResponse:
    """Record the uploaded blob URL; a consumed row discards the new blob."""
    download = await download_service.mark_ready(download_id, request.blob_url)
    await session.commit()
    return InternalStatusResponse(status=download.status)


@router.post(
    "/internal/downloads/{download_id}/failed",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_fail_download(
    download_id: UUID,
    request: InternalDownloadFailRequest,
    download_service: VideoDownloadService = Depends(_get_download_service),
    session: AsyncSession = Depends(get_db_session),
) -> InternalStatusResponse:
    """Mark a download as failed with a user-safe error message."""
    await download_service.get_by_id(download_id)
    download = await download_service.mark_failed(download_id, request.error)
    await session.commit()
    return InternalStatusResponse(status=download.status)


@router.get(
    "/internal/notes/{note_id}",
    response_model=InternalNotesResponse,
    dependencies=[Depends(_verify_internal_api_key)],
    responses={401: {"description": "Invalid API key"}, 404: {"description": "Notes not found"}},
)
async def internal_get_notes(
    note_id: UUID,
    notes_service: VideoNotesService = Depends(_get_notes_service),
    word_repo: TranscriptWordRepository = Depends(_get_word_repo),
) -> InternalNotesResponse:
    """Return the video and timestamped transcript needed to generate notes."""
    note, video = await notes_service.get_for_worker(note_id)
    words = await word_repo.list_all_for_video(video.id)
    lines = assemble_lines(words)
    return InternalNotesResponse(
        note_id=note.id,
        video_id=note.video_id,
        youtube_video_id=video.video_id,
        youtube_url=video.video_url,
        title=video.title or video.video_id,
        duration_seconds=video.duration_seconds,
        language=video.language,
        is_rtl=is_rtl_transcript(words, video.language),
        status=note.status,
        transcript_lines=[{"seconds": line.seconds, "text": line.text} for line in lines],
    )


@router.post(
    "/internal/notes/{note_id}/advance",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_advance_notes(
    note_id: UUID,
    notes_service: VideoNotesService = Depends(_get_notes_service),
) -> InternalStatusResponse:
    """Mark a lecture-notes request as processing."""
    note = await notes_service.mark_processing(note_id)
    return InternalStatusResponse(status=note.status)


@router.post(
    "/internal/notes/{note_id}/ready",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_mark_notes_ready(
    note_id: UUID,
    request: InternalDownloadReadyRequest,
    notes_service: VideoNotesService = Depends(_get_notes_service),
    session: AsyncSession = Depends(get_db_session),
) -> InternalStatusResponse:
    """Record the uploaded lecture-notes PDF URL."""
    note = await notes_service.mark_ready(note_id, request.blob_url)
    await session.commit()
    return InternalStatusResponse(status=note.status)


@router.post(
    "/internal/notes/{note_id}/failed",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_fail_notes(
    note_id: UUID,
    request: InternalDownloadFailRequest,
    notes_service: VideoNotesService = Depends(_get_notes_service),
    session: AsyncSession = Depends(get_db_session),
) -> InternalStatusResponse:
    """Mark a lecture-notes request as failed with a user-safe message."""
    note = await notes_service.mark_failed(note_id, request.error)
    await session.commit()
    return InternalStatusResponse(status=note.status)


@router.post(
    "/internal/videos/{youtube_video_id}/formats",
    response_model=InternalStatusResponse,
    dependencies=[Depends(_verify_internal_api_key)],
)
async def internal_store_video_formats(
    youtube_video_id: str,
    request: InternalVideoFormatsRequest,
    video_repo: VideoRepository = Depends(_get_video_repo),
    session: AsyncSession = Depends(get_db_session),
) -> InternalStatusResponse:
    """Cache the H.264 quality ladder the worker probed for a video."""
    video = await video_repo.get_by_video_id_lite(youtube_video_id)
    if not video:
        raise VideoNotFoundError(youtube_video_id)
    await video_repo.update_available_heights(video.id, request.heights)
    await session.commit()
    return InternalStatusResponse(status="stored")
