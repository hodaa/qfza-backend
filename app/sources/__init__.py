"""Video sources package exports."""

from app.sources.base import SourceCapabilities, VideoRef, VideoSource
from app.sources.registry import SOURCES, identify_video, resolve_source

__all__ = [
    "SOURCES",
    "SourceCapabilities",
    "VideoRef",
    "VideoSource",
    "identify_video",
    "resolve_source",
]
