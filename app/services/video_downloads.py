"""Offloaded video downloads: the request ledger and the formats cache.

The backend no longer runs yt-dlp for downloads. It queues ``video_downloads``
rows; the worker - which holds the cookies, the residential IP and the media
tooling the serverless runtime lacks - produces the MP4, uploads it to blob
storage, and reports the blob URL back. These services carry that URL to the
polling client.

Downloads are one-shot: the blob is deleted when the client consumes it, and a
retention sweep runs on every create so abandoned requests (and the blobs they
uploaded) cannot accumulate without a scheduler.

Format listings are served from the column the worker probe fills, so the
common case is a database read; a miss queues a probe and waits briefly.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DownloadNotFoundError, ValidationError, VideoNotFoundError
from app.core.logging import get_logger
from app.models import Video, VideoDownload
from app.repositories import VideoDownloadRepository, VideoRepository
from app.services.blob_store import delete_blob_url
from app.services.messaging import dispatch_formats_probe

logger = get_logger(__name__)

#: Download lifecycle states, mirrored by the API schemas.
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_CONSUMED = "consumed"

#: A worker that died between advance and report leaves a row behind; past this
#: age the row is failed so a polling client stops waiting.
DOWNLOAD_STALE_SECONDS = 1800

#: How long a finished row is kept past its last change before the sweep
#: removes it, deleting the blob first when one is still attached.
DOWNLOAD_RETENTION_SECONDS = 86400

#: Rows retired per sweep call. The sweep runs on every create, so a backlog
#: drains over a few requests rather than in one slow pass.
SWEEP_BATCH_LIMIT = 10

#: User-safe failure recorded for a download the sweep gave up on.
SWEEP_FAILURE_MESSAGE = "The download timed out. Please try again."

#: The formats wait: how long a cache-miss request holds the client while the
#: worker probes, and how often the cached row is re-read. A timeout answers
#: "no qualities" rather than an error: the download then falls back to its own
#: best stream, which is what an unbounded wait would have produced anyway.
FORMATS_WAIT_SECONDS = 15
FORMATS_POLL_INTERVAL_SECONDS = 1.0

#: How long a probed ladder is trusted before a view quietly queues a re-probe
#: (the stale answer is still served immediately).
FORMATS_REFRESH_TTL_SECONDS = 86400

_FILE_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def download_filename(
    youtube_video_id: str,
    *,
    prefix: str = "video",
    suffix: str = ".mp4",
    range_start: int | None = None,
    range_end: int | None = None,
    height: int | None = None,
) -> str:
    """Build a download filename that is safe to place in a response header."""
    safe_id = _FILE_SAFE.sub("", youtube_video_id)[:64] or "video"
    label = f"-{height}p" if height is not None else ""
    if range_start is not None:
        label += f"-{range_start}-{range_end if range_end is not None else 'end'}"
    return f"{prefix}-{safe_id}{label}{suffix}"


def validated_range(
    start: float | None, end: float | None, duration_seconds: int | None
) -> tuple[int | None, int | None]:
    """Validate a requested download range against the video's duration.

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


class VideoDownloadService:
    """Owns download rows from queueing to the blob's deletion."""

    def __init__(
        self,
        video_repo: VideoRepository,
        download_repo: VideoDownloadRepository,
        blob_deleter: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Wire the service; the blob deleter is swappable for tests."""
        self._video_repo = video_repo
        self._download_repo = download_repo
        self._blob_deleter = blob_deleter or delete_blob_url

    async def create(
        self,
        youtube_video_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
        height: int | None = None,
    ) -> VideoDownload:
        """Queue a download for an existing video, retiring old rows first."""
        video = await _require_video(self._video_repo, youtube_video_id)
        start_seconds, end_seconds = validated_range(start, end, video.duration_seconds)
        await self._sweep_quietly()
        return await self._download_repo.create(
            video_id=video.id,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            height=height,
        )

    async def get(self, youtube_video_id: str, download_id: UUID) -> VideoDownload:
        """Return one download scoped to its video, or 404."""
        video = await _require_video(self._video_repo, youtube_video_id)
        download = await self._download_repo.get_for_video(download_id, video.id)
        if download is None:
            raise DownloadNotFoundError(str(download_id))
        return download

    async def get_by_id(self, download_id: UUID) -> VideoDownload:
        """Return a download by id, or 404."""
        download = await self._download_repo.get_by_id(download_id)
        if download is None:
            raise DownloadNotFoundError(str(download_id))
        return download

    async def get_for_worker(self, download_id: UUID) -> tuple[VideoDownload, Video]:
        """Return a download and its video, for the worker's fetch."""
        download = await self.get_by_id(download_id)
        video = await self._video_repo.get_by_id_lite(download.video_id)
        if video is None:
            raise VideoNotFoundError(str(download.video_id))
        return download, video

    async def consume(self, youtube_video_id: str, download_id: UUID) -> VideoDownload:
        """Delete the uploaded blob, if any, and mark the row consumed.

        Idempotent. A failed blob delete is logged and the row is still
        consumed: the client already has the file, and the retention sweep
        retries the deletion later, so their success never hinges on it.
        """
        download = await self.get(youtube_video_id, download_id)
        if download.status == STATUS_CONSUMED:
            return download
        await self._delete_blob_quietly(download.blob_url)
        return await self._download_repo.update_status(
            download.id, status=STATUS_CONSUMED, consumed=True
        )

    async def mark_processing(self, download_id: UUID) -> VideoDownload:
        """Advance a pending download to processing."""
        return await self._download_repo.update_status(download_id, status=STATUS_PROCESSING)

    async def mark_ready(self, download_id: UUID, blob_url: str) -> VideoDownload:
        """Publish a finished upload, discarding it when the client gave up first.

        A row the client already consumed has no reader for this blob; deleting
        it here closes the race where the upload lands after the consume.
        """
        download = await self.get_by_id(download_id)
        if download.status == STATUS_CONSUMED:
            await self._delete_blob_quietly(blob_url)
            return download
        return await self._download_repo.update_status(
            download_id, status=STATUS_READY, blob_url=blob_url
        )

    async def mark_failed(self, download_id: UUID, error: str) -> VideoDownload:
        """Record a worker-reported failure with its user-safe message."""
        return await self._download_repo.update_status(
            download_id, status=STATUS_FAILED, error=error
        )

    async def _sweep_quietly(self) -> None:
        """Run the sweep without ever failing the request that triggered it."""
        try:
            await self._sweep()
        except Exception:
            logger.warning("Download sweep failed", exc_info=True)

    async def _sweep(self) -> None:
        """Fail rows a dead worker left active, and remove rows past retention.

        Removing an expired row deletes its blob first when one is still
        attached, so a consume whose blob delete failed (or a client that
        never consumed at all) still gets its storage back.
        """
        stale_cutoff = datetime.now(UTC) - timedelta(seconds=DOWNLOAD_STALE_SECONDS)
        for row in await self._download_repo.list_stale_active(stale_cutoff, SWEEP_BATCH_LIMIT):
            await self._download_repo.update_status(
                row.id, status=STATUS_FAILED, error=SWEEP_FAILURE_MESSAGE
            )
        expired_cutoff = datetime.now(UTC) - timedelta(seconds=DOWNLOAD_RETENTION_SECONDS)
        expired = await self._download_repo.list_expired(expired_cutoff, SWEEP_BATCH_LIMIT)
        for row in expired:
            await self._delete_blob_quietly(row.blob_url)
        if expired:
            await self._download_repo.delete_ids([row.id for row in expired])

    async def _delete_blob_quietly(self, blob_url: str | None) -> None:
        """Best-effort blob deletion: storage being down must not break a client."""
        if not blob_url:
            return
        try:
            await self._blob_deleter(blob_url)
        except Exception:
            logger.warning("Blob delete failed", blob_url=blob_url, exc_info=True)


class VideoFormatsService:
    """Serves quality ladders from the cache, probing through the worker on a miss."""

    def __init__(
        self,
        video_repo: VideoRepository,
        session: AsyncSession,
        dispatch: Callable[[str, str], None] = dispatch_formats_probe,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wire the service; the sleep is swappable so tests do not wait."""
        self._video_repo = video_repo
        self._session = session
        self._dispatch = dispatch
        self._sleep = sleep

    async def get_heights(self, youtube_video_id: str) -> list[int]:
        """Return the cached quality ladder, probing once when none is stored.

        A stale ladder is still served immediately; a refresh is only queued,
        so a visitor never waits for a probe they did not ask for.
        """
        video = await _require_video(self._video_repo, youtube_video_id)
        if video.available_heights is not None:
            if _ladder_is_stale(video.formats_refreshed_at):
                await self._dispatch_quietly(video)
            return list(video.available_heights)
        if not await self._dispatch_quietly(video):
            return []
        return await self._wait_for_probe(video)

    async def _dispatch_quietly(self, video: Video) -> bool:
        """Queue a probe; a broker failure only means "no fresh answer"."""
        try:
            await asyncio.to_thread(self._dispatch, video.video_id, video.youtube_url)
            return True
        except Exception:
            logger.warning(
                "Formats probe dispatch failed", youtube_video_id=video.video_id, exc_info=True
            )
            return False

    async def _wait_for_probe(self, video: Video) -> list[int]:
        """Hold the client briefly while the worker probes; timeout means empty.

        An empty list is the documented "no qualities" answer: the picker
        hides and the download falls back to its own best stream - the same
        outcome a longer wait would have produced if the probe never answered.
        """
        for _ in range(int(FORMATS_WAIT_SECONDS / FORMATS_POLL_INTERVAL_SECONDS)):
            await self._sleep(FORMATS_POLL_INTERVAL_SECONDS)
            await self._session.refresh(video)
            if video.formats_refreshed_at is not None:
                return list(video.available_heights or [])
        logger.info("Formats probe timed out", youtube_video_id=video.video_id)
        return []


async def _require_video(video_repo: VideoRepository, youtube_video_id: str) -> Video:
    """Resolve the video or raise the 404 the API contract expects."""
    video = await video_repo.get_by_video_id_lite(youtube_video_id)
    if video is None:
        raise VideoNotFoundError(youtube_video_id)
    return video


def _ladder_is_stale(refreshed_at: datetime | None) -> bool:
    """Whether a cached ladder is old enough to re-probe in the background."""
    if refreshed_at is None:
        return True
    return datetime.now(UTC) - refreshed_at > timedelta(seconds=FORMATS_REFRESH_TTL_SECONDS)
