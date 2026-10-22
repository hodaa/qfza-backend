"""The registry: which sources exist and which one owns a URL."""

from collections.abc import Sequence

from app.core.exceptions import UnsupportedVideoSourceError
from app.sources.base import VideoRef, VideoSource
from app.sources.drive import GoogleDriveSource
from app.sources.youtube import YouTubeSource

SOURCES: tuple[VideoSource, ...] = (YouTubeSource(), GoogleDriveSource())


def resolve_source(url: str, sources: Sequence[VideoSource] = SOURCES) -> VideoSource:
    """Return the source that claims the URL, or raise for an unknown host."""
    for source in sources:
        if source.matches(url):
            return source
    raise UnsupportedVideoSourceError(url)


def identify_video(url: str, sources: Sequence[VideoSource] = SOURCES) -> VideoRef:
    """Parse a pasted URL into a source-agnostic VideoRef."""
    return resolve_source(url, sources).identify(url)
