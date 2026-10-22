"""The contract a video source must satisfy, and the value it identifies."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VideoRef:
    """A video identified on one source.

    ``source_id`` is the id the source itself uses (a YouTube video id, a Drive
    file id). ``canonical_url`` is the clean URL the pipeline stores and reads
    back, with tracking and playlist parameters dropped.
    """

    source: str
    source_id: str
    canonical_url: str


@dataclass(frozen=True)
class SourceCapabilities:
    """What the app can do with a source's videos.

    Flags are added here when a consumer needs them, so each one states a real
    behaviour rather than a guess about the host.
    """

    qualities: bool = False


class VideoSource(Protocol):
    """One video host: how to recognise its URLs and what they support."""

    name: str
    capabilities: SourceCapabilities

    def matches(self, url: str) -> bool:
        """Whether this source claims the URL's host."""
        ...

    def identify(self, url: str) -> VideoRef:
        """Parse the URL into a VideoRef, or raise when it is malformed."""
        ...
