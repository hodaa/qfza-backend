"""Download a video's media file from YouTube with yt-dlp.

YouTube serves video and audio as separate streams, so the file has to be
merged, and merging needs a seekable destination: a merge piped straight to the
client silently falls back to a stream container instead of the MP4 that was
asked for. The download therefore lands in a temporary directory and is served
from there, and the caller removes that directory once the file has been sent.

The merge needs ffmpeg beside yt-dlp. A runtime that carries no system ffmpeg -
the serverless deploy, for one - falls back to the static binary shipped inside
the imageio-ffmpeg wheel before refusing the download up front rather than
failing halfway through the response.

The serverless runtime also assembles the app's sys.path without exporting it,
so a plain child interpreter cannot import yt-dlp; the child is handed the
package directory through PYTHONPATH instead.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from app.core.exceptions import ExternalServiceError, VideoDownloadUnavailableError
from app.core.logging import get_logger
from app.services.youtube import normalize_youtube_url

logger = get_logger(__name__)

#: H.264 plus AAC where possible - the codecs QuickTime, iOS and TVs decode.
#: YouTube's better-compressed VP9/AV1 and Opus streams play as audio-only in
#: Apple's players. A pre-merged MP4, then the best streams, are the fallbacks.
FORMAT_SELECTOR = "bv*[vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"

#: Container the merged streams are written to.
MERGE_FORMAT = "mp4"

#: Bounds of the H.264 quality ladder worth offering. Below the floor no useful
#: picture remains, and above the ceiling YouTube serves only VP9/AV1, which
#: Apple players cannot decode as video.
MIN_VIDEO_HEIGHT = 144
MAX_AVC1_HEIGHT = 1080

#: Longest a download may run before it is killed. A feature-length video takes
#: minutes, so this only stops a transfer that has stopped making progress.
DOWNLOAD_TIMEOUT_SECONDS = 600

#: Longest a metadata probe may run; listing qualities is not a download.
PROBE_TIMEOUT_SECONDS = 60

#: A download failing within this many seconds is treated as a transient
#: refusal - YouTube intermittently answers the media URLs with HTTP 403 - and
#: is retried once with a fresh extraction.
_RETRY_BELOW_SECONDS = 60.0

#: Pause before retrying a fast failure.
_RETRY_DELAY_SECONDS = 2.0

#: Exit code reported in place of yt-dlp's when the timeout killed it.
_TIMEOUT_EXIT_CODE = -1

#: How much of yt-dlp's stderr to keep in the log when a download fails.
_STDERR_TAIL_CHARS = 1000

_TEMP_PREFIX = "qfza-video-"
_DOWNLOAD_STEM = "video"
_YT_DLP_MODULE = "yt_dlp"
_FFMPEG_BINARY = "ffmpeg"
_FFMPEG_PACKAGE = "imageio_ffmpeg"


def _bundled_ffmpeg() -> str | None:
    """Path to the static ffmpeg inside its Python package, if one is installed.

    The serverless deploy carries no system ffmpeg; imageio-ffmpeg ships a
    static binary in the wheel so the merge still has a tool to run.
    """
    try:
        package = importlib.import_module(_FFMPEG_PACKAGE)
    except ImportError:
        return None
    try:
        return package.get_ffmpeg_exe()
    except Exception:
        logger.warning("Bundled ffmpeg is installed but unusable")
        return None


def _ffmpeg_location() -> str | None:
    """The --ffmpeg-location value, or None when yt-dlp finds ffmpeg itself."""
    if shutil.which(_FFMPEG_BINARY) is not None:
        return None
    return _bundled_ffmpeg()


def _missing_tool() -> str | None:
    """Name the first media tool this runtime does not have, if any."""
    if importlib.util.find_spec(_YT_DLP_MODULE) is None:
        return _YT_DLP_MODULE
    if shutil.which(_FFMPEG_BINARY) is None and _bundled_ffmpeg() is None:
        return _FFMPEG_BINARY
    return None


def _format_section(start_seconds: int, end_seconds: int | None) -> str:
    """Format a ``--download-sections`` range; an omitted end runs to the end."""
    end = end_seconds if end_seconds is not None else "inf"
    return f"*{start_seconds}-{end}"


def _format_selector(height: int | None) -> str:
    """Build the format selector, capped at ``height`` when one is asked for.

    The capped ladder keeps the documented preference order and ends with the
    uncapped fallback, so a video without that height still yields a file.
    """
    if height is None:
        return FORMAT_SELECTOR
    return (
        f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]"
        f"/b[height<={height}][ext=mp4]"
        "/bv*+ba/b"
    )


def _build_command(
    youtube_video_id: str,
    destination: Path,
    section: str | None = None,
    height: int | None = None,
    ffmpeg_location: str | None = None,
) -> list[str]:
    """Build the yt-dlp command that downloads one video to ``destination``."""
    command = [
        sys.executable,
        "-m",
        _YT_DLP_MODULE,
        "-f",
        _format_selector(height),
        "--merge-output-format",
        MERGE_FORMAT,
        "--no-playlist",
    ]
    if ffmpeg_location is not None:
        command.extend(["--ffmpeg-location", ffmpeg_location])
    if section is not None:
        command.extend(["--download-sections", section])
    command.extend(["-o", str(destination), normalize_youtube_url(youtube_video_id)])
    return command


def _dependency_env() -> dict[str, str]:
    """Environment for the yt-dlp child that can import the app's packages.

    The serverless runtime puts its package directory on sys.path without
    exporting it, so a freshly spawned interpreter cannot import yt-dlp; point
    the child's PYTHONPATH at the directory that holds the package.
    """
    env = dict(os.environ)
    spec = importlib.util.find_spec(_YT_DLP_MODULE)
    if spec is None or not spec.origin:
        return env
    directory = str(Path(spec.origin).parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{directory}{os.pathsep}{existing}" if existing else directory
    return env


async def _run_yt_dlp(command: list[str]) -> tuple[int, bytes]:
    """Run yt-dlp to completion, returning its exit code and stderr."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=_dependency_env(),
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), DOWNLOAD_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return _TIMEOUT_EXIT_CODE, b"timed out"
    return process.returncode or 0, stderr


def _clear(directory: Path) -> None:
    """Remove downloaded files so a retry starts from scratch."""
    for child in directory.iterdir():
        child.unlink()


def _log_failed_attempt(youtube_video_id: str, returncode: int, stderr: bytes) -> None:
    """Log a failed attempt so a retry is visible in the logs."""
    logger.warning(
        "Video download attempt failed",
        youtube_video_id=youtube_video_id,
        returncode=returncode,
        stderr=stderr.decode(errors="replace")[-_STDERR_TAIL_CHARS:],
    )


async def _download_with_retry(
    youtube_video_id: str,
    directory: Path,
    destination: Path,
    section: str | None = None,
    height: int | None = None,
    ffmpeg_location: str | None = None,
) -> tuple[int, bytes]:
    """Download the video, retrying once when the first attempt fails fast."""
    started = time.monotonic()
    command = _build_command(youtube_video_id, destination, section, height, ffmpeg_location)
    returncode, stderr = await _run_yt_dlp(command)
    if returncode == 0 and destination.exists():
        return returncode, stderr
    _log_failed_attempt(youtube_video_id, returncode, stderr)
    if time.monotonic() - started > _RETRY_BELOW_SECONDS:
        return returncode, stderr
    _clear(directory)
    await asyncio.sleep(_RETRY_DELAY_SECONDS)
    return await _run_yt_dlp(command)


def _discard(directory: Path, youtube_video_id: str, returncode: int, stderr: bytes) -> None:
    """Remove a failed download's directory and log why it failed."""
    shutil.rmtree(directory, ignore_errors=True)
    logger.warning(
        "Video download failed",
        youtube_video_id=youtube_video_id,
        returncode=returncode,
        stderr=stderr.decode(errors="replace")[-_STDERR_TAIL_CHARS:],
    )


async def download_video_file(
    youtube_video_id: str,
    *,
    start_seconds: int | None = None,
    end_seconds: int | None = None,
    height: int | None = None,
) -> Path:
    """Download a video as MP4 and return its path in a fresh temp directory.

    When ``start_seconds`` is given only that section is downloaded, up to
    ``end_seconds`` or to the end of the video when it is omitted. When
    ``height`` is given the file is capped at that H.264 quality.

    The caller owns that directory and must remove it on every path once the
    file has been served.
    """
    missing = _missing_tool()
    if missing:
        logger.warning("Video download tooling missing", tool=missing)
        raise VideoDownloadUnavailableError()
    ffmpeg_location = _ffmpeg_location()
    section = _format_section(start_seconds, end_seconds) if start_seconds is not None else None
    directory = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX))
    destination = directory / f"{_DOWNLOAD_STEM}.{MERGE_FORMAT}"
    returncode, stderr = await _download_with_retry(
        youtube_video_id, directory, destination, section, height, ffmpeg_location
    )
    if returncode == 0 and destination.exists():
        return destination
    _discard(directory, youtube_video_id, returncode, stderr)
    raise ExternalServiceError("Could not download the video", service=_YT_DLP_MODULE)


def _build_probe_command(youtube_video_id: str) -> list[str]:
    """Build the yt-dlp command that prints one video's metadata as JSON."""
    return [
        sys.executable,
        "-m",
        _YT_DLP_MODULE,
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        normalize_youtube_url(youtube_video_id),
    ]


async def _run_probe(command: list[str]) -> tuple[int, bytes, bytes]:
    """Run a metadata-only yt-dlp command, returning code, stdout and stderr."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_dependency_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return _TIMEOUT_EXIT_CODE, b"", b"timed out"
    return process.returncode or 0, stdout, stderr


def _heights_from_info(payload: bytes) -> list[int]:
    """Collect the distinct H.264 heights in a probe payload, tallest first."""
    try:
        info = json.loads(payload)
    except ValueError:
        logger.warning("Video format probe returned unreadable JSON")
        return []
    if not isinstance(info, dict):
        return []
    heights: set[int] = set()
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        height = fmt.get("height")
        codec = str(fmt.get("vcodec") or "")
        if (
            codec.startswith("avc1")
            and isinstance(height, int)
            and MIN_VIDEO_HEIGHT <= height <= MAX_AVC1_HEIGHT
        ):
            heights.add(height)
    return sorted(heights, reverse=True)


async def list_video_heights(youtube_video_id: str) -> list[int]:
    """List the video's available H.264 download heights, tallest first.

    A metadata-only probe: no media is fetched. An empty list means the video
    names no H.264 quality; the download then uses its own best fallback.
    """
    missing = _missing_tool()
    if missing:
        logger.warning("Video format probe tooling missing", tool=missing)
        raise VideoDownloadUnavailableError()
    returncode, stdout, stderr = await _run_probe(_build_probe_command(youtube_video_id))
    if returncode != 0:
        logger.warning(
            "Video format probe failed",
            youtube_video_id=youtube_video_id,
            returncode=returncode,
            stderr=stderr.decode(errors="replace")[-_STDERR_TAIL_CHARS:],
        )
        raise ExternalServiceError("Could not list the video's formats", service=_YT_DLP_MODULE)
    return _heights_from_info(stdout)
