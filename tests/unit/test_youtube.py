"""Unit tests for YouTube URL parsing."""

import pytest

from app.core.exceptions import InvalidYouTubeURLError
from app.sources.base import VideoRef
from app.sources.youtube import (
    YouTubeSource,
    extract_video_id,
    is_youtube_url,
    normalize_youtube_url,
    validate_youtube_url,
)


class TestExtractVideoId:
    """Tests for extract_video_id function."""

    def test_standard_watch_url(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_short_url(self) -> None:
        url = "https://youtu.be/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_shorts_url(self) -> None:
        url = "https://www.youtube.com/shorts/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_embed_url(self) -> None:
        url = "https://www.youtube.com/embed/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_url_with_extra_params(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=30s"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_url_with_si_param(self) -> None:
        url = "https://youtu.be/dQw4w9WgXcQ?si=abc123"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_non_youtube_url(self) -> None:
        url = "https://example.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) is None

    def test_invalid_url(self) -> None:
        url = "not a url"
        assert extract_video_id(url) is None


class TestIsYouTubeUrl:
    """Tests for is_youtube_url function."""

    def test_valid_watch_url(self) -> None:
        assert is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is True

    def test_valid_short_url(self) -> None:
        assert is_youtube_url("https://youtu.be/dQw4w9WgXcQ") is True

    def test_valid_shorts_url(self) -> None:
        assert is_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ") is True

    def test_valid_embed_url(self) -> None:
        assert is_youtube_url("https://www.youtube.com/embed/dQw4w9WgXcQ") is True

    def test_rejected_channel_url(self) -> None:
        assert is_youtube_url("https://www.youtube.com/channel/UC123") is False

    def test_rejected_user_url(self) -> None:
        assert is_youtube_url("https://www.youtube.com/user/username") is False

    def test_rejected_watch_without_v(self) -> None:
        assert is_youtube_url("https://www.youtube.com/watch") is False

    def test_rejected_non_youtube(self) -> None:
        assert is_youtube_url("https://vimeo.com/123456") is False


class TestNormalizeYoutubeUrl:
    """Tests for normalize_youtube_url function."""

    def test_builds_canonical_watch_url(self) -> None:
        assert normalize_youtube_url("dQw4w9WgXcQ") == (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )


class TestValidateYouTubeUrl:
    """Tests for validate_youtube_url function."""

    def test_valid_url_returns_video_ref(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = validate_youtube_url(url)
        assert isinstance(result, VideoRef)
        assert result.source == "youtube"
        assert result.source_id == "dQw4w9WgXcQ"
        assert result.canonical_url == normalize_youtube_url("dQw4w9WgXcQ")

    def test_playlist_params_are_stripped(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=3"
        result = validate_youtube_url(url)
        assert result.canonical_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_tracking_params_are_stripped(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=abc123&t=30s"
        result = validate_youtube_url(url)
        assert result.canonical_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_invalid_url_raises_exception(self) -> None:
        url = "https://example.com/video"
        with pytest.raises(InvalidYouTubeURLError) as exc_info:
            validate_youtube_url(url)
        assert exc_info.value.details["url"] == url

    def test_channel_url_raises_exception(self) -> None:
        url = "https://www.youtube.com/channel/UC123"
        with pytest.raises(InvalidYouTubeURLError):
            validate_youtube_url(url)

    def test_watch_without_v_raises_exception(self) -> None:
        url = "https://www.youtube.com/watch"
        with pytest.raises(InvalidYouTubeURLError):
            validate_youtube_url(url)


class TestYouTubeSource:
    """Tests for the YouTube source strategy."""

    def test_name_and_capabilities(self) -> None:
        source = YouTubeSource()
        assert source.name == "youtube"
        assert source.capabilities.qualities is True

    def test_matches_any_youtube_host(self) -> None:
        source = YouTubeSource()
        assert source.matches("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is True
        assert source.matches("https://youtu.be/dQw4w9WgXcQ") is True
        # A channel page is not a video, but it is still YouTube's to reject.
        assert source.matches("https://www.youtube.com/channel/UC123") is True
        assert source.matches("https://drive.google.com/file/d/abc/view") is False

    def test_identify_returns_video_ref(self) -> None:
        source = YouTubeSource()
        ref = source.identify("https://youtu.be/dQw4w9WgXcQ")
        assert ref == VideoRef(
            source="youtube",
            source_id="dQw4w9WgXcQ",
            canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
