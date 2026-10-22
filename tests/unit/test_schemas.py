"""Unit tests for request-schema contracts."""

import pytest
from pydantic import ValidationError

from app.schemas import SearchRequest

_YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_search_request_accepts_video_url() -> None:
    """The canonical request field is named video_url."""
    request = SearchRequest(video_url=_YOUTUBE_URL, keyword="test")
    assert str(request.video_url) == _YOUTUBE_URL


def test_search_request_accepts_legacy_youtube_url() -> None:
    """The pre-rename field name keeps working for existing clients."""
    request = SearchRequest(youtube_url=_YOUTUBE_URL, keyword="test")
    assert str(request.video_url) == _YOUTUBE_URL


def test_search_request_rejects_invalid_url() -> None:
    """A value that is not a URL fails validation under either name."""
    with pytest.raises(ValidationError):
        SearchRequest(video_url="not-a-url", keyword="test")

    with pytest.raises(ValidationError):
        SearchRequest(youtube_url="not-a-url", keyword="test")
