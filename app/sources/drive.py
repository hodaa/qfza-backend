"""Google Drive file links: id extraction and the Drive video source."""

import re
from urllib.parse import parse_qs, urlsplit

from app.core.exceptions import InvalidVideoURLError
from app.core.logging import get_logger
from app.sources.base import SourceCapabilities, VideoRef

logger = get_logger(__name__)

HOST = "drive.google.com"

# Drive file ids are opaque and longer than YouTube's 11; they are 25+ chars
# and met in practice at 28 or 33. A Drive id can never collide with an 11-char
# YouTube id, so the accepted charset is shared but the length is not.
FILE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{25,}")


def normalize_drive_url(file_id: str) -> str:
    """Build the canonical Drive file URL for a file id.

    Every accepted link shape (/file/d/<id>, /open?id=<id>, /uc?id=<id>) maps
    onto this one form, so a video row always stores the same URL for the same
    file no matter which link the visitor pasted.
    """
    return f"https://{HOST}/file/d/{file_id}"


def is_drive_url(url: str) -> bool:
    """Whether the URL points at the Drive host."""
    return urlsplit(url).netloc.lower() == HOST


def extract_file_id(url: str) -> str | None:
    """Extract a Drive file id from /file/d/<id> paths or ?id=<id> queries.

    Returns None for anything else, including folder links: a folder is not a
    video, and claiming it here would only move the failure downstream.
    """
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) >= 3 and segments[0] == "file" and segments[1] == "d":
        return segments[2] if FILE_ID_PATTERN.fullmatch(segments[2]) else None
    query_ids = parse_qs(parts.query).get("id")
    if query_ids and FILE_ID_PATTERN.fullmatch(query_ids[0]):
        return query_ids[0]
    return None


class GoogleDriveSource:
    """Drive files. Searchable through our own transcription; no quality ladder.

    Drive exposes no per-resolution formats to choose from, so the download
    falls back to the file as uploaded, and ``qualities`` is off.
    """

    name: str = "drive"
    capabilities: SourceCapabilities = SourceCapabilities(qualities=False)

    def matches(self, url: str) -> bool:
        """Whether the URL points at the Drive host."""
        return is_drive_url(url)

    def identify(self, url: str) -> VideoRef:
        """Parse the URL into a VideoRef, or raise when it is malformed."""
        file_id = extract_file_id(url)
        if not file_id:
            logger.warning("Could not extract a Drive file id from URL", url=url)
            raise InvalidVideoURLError(url)
        logger.debug("Parsed Drive URL", file_id=file_id, url=url)
        return VideoRef(
            source=self.name,
            source_id=file_id,
            canonical_url=normalize_drive_url(file_id),
        )
