"""Unit tests for the video sources registry and the Drive strategy."""

import pytest

from app.core.exceptions import (
    InvalidVideoURLError,
    InvalidYouTubeURLError,
    UnsupportedVideoSourceError,
)
from app.sources import SOURCES, identify_video, resolve_source
from app.sources.base import VideoRef
from app.sources.drive import (
    GoogleDriveSource,
    extract_file_id,
    is_drive_url,
    normalize_drive_url,
)

FILE_ID = "1DOoJhC6NHpLSu2nAvaWy3HukVimEyyhE"
CANONICAL = f"https://drive.google.com/file/d/{FILE_ID}"
SHARED_LINK = f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class TestExtractFileId:
    """Tests for extract_file_id."""

    def test_file_view_url(self) -> None:
        assert extract_file_id(f"{CANONICAL}/view") == FILE_ID

    def test_file_view_url_with_tracking_params(self) -> None:
        assert extract_file_id(f"{CANONICAL}/view?usp=sharing") == FILE_ID

    def test_open_id_query(self) -> None:
        assert extract_file_id(f"https://drive.google.com/open?id={FILE_ID}") == FILE_ID

    def test_uc_export_query(self) -> None:
        url = f"https://drive.google.com/uc?export=download&id={FILE_ID}"
        assert extract_file_id(url) == FILE_ID

    def test_folder_url_is_not_a_file(self) -> None:
        assert extract_file_id("https://drive.google.com/drive/folders/abc") is None

    def test_short_id_is_rejected(self) -> None:
        assert extract_file_id("https://drive.google.com/file/d/abc123/view") is None

    def test_missing_id_returns_none(self) -> None:
        assert extract_file_id("https://drive.google.com") is None


class TestDriveUrlHelpers:
    """Tests for is_drive_url and normalize_drive_url."""

    def test_drive_host_matches(self) -> None:
        assert is_drive_url("https://drive.google.com/file/d/x/view") is True

    def test_other_hosts_do_not_match(self) -> None:
        assert is_drive_url(YOUTUBE) is False
        assert is_drive_url("https://example.com") is False

    def test_normalize_builds_canonical_file_url(self) -> None:
        assert normalize_drive_url(FILE_ID) == CANONICAL


class TestGoogleDriveSource:
    """Tests for the Drive source strategy."""

    def test_name_and_capabilities(self) -> None:
        source = GoogleDriveSource()
        assert source.name == "drive"
        assert source.capabilities.qualities is False

    def test_matches_drive_urls_only(self) -> None:
        source = GoogleDriveSource()
        assert source.matches(SHARED_LINK) is True
        assert source.matches(YOUTUBE) is False

    def test_identify_returns_canonical_ref(self) -> None:
        source = GoogleDriveSource()
        ref = source.identify(SHARED_LINK)
        assert ref == VideoRef(source="drive", source_id=FILE_ID, canonical_url=CANONICAL)

    def test_folder_link_raises(self) -> None:
        source = GoogleDriveSource()
        with pytest.raises(InvalidVideoURLError) as exc_info:
            source.identify("https://drive.google.com/drive/folders/abc")
        assert exc_info.value.details["url"] == "https://drive.google.com/drive/folders/abc"

    def test_bad_id_raises(self) -> None:
        source = GoogleDriveSource()
        with pytest.raises(InvalidVideoURLError):
            source.identify("https://drive.google.com/file/d/abc/view")


class TestRegistry:
    """Tests for resolve_source and identify_video."""

    def test_sources_cover_youtube_then_drive(self) -> None:
        assert [source.name for source in SOURCES] == ["youtube", "drive"]

    def test_resolve_youtube(self) -> None:
        assert resolve_source(YOUTUBE).name == "youtube"

    def test_resolve_drive(self) -> None:
        assert resolve_source(SHARED_LINK).name == "drive"

    def test_unknown_host_raises(self) -> None:
        with pytest.raises(UnsupportedVideoSourceError) as exc_info:
            resolve_source("https://vimeo.com/123456")
        assert exc_info.value.details["url"] == "https://vimeo.com/123456"

    def test_identify_youtube_returns_canonical_watch_url(self) -> None:
        ref = identify_video("https://youtu.be/dQw4w9WgXcQ")
        assert ref == VideoRef(source="youtube", source_id="dQw4w9WgXcQ", canonical_url=YOUTUBE)

    def test_identify_drive_returns_canonical_file_url(self) -> None:
        ref = identify_video(SHARED_LINK)
        assert ref == VideoRef(source="drive", source_id=FILE_ID, canonical_url=CANONICAL)

    def test_identify_unsupported_host_raises(self) -> None:
        with pytest.raises(UnsupportedVideoSourceError):
            identify_video("https://vimeo.com/123456")

    def test_identify_youtube_channel_raises_invalid_youtube(self) -> None:
        with pytest.raises(InvalidYouTubeURLError):
            identify_video("https://www.youtube.com/channel/UC123")
