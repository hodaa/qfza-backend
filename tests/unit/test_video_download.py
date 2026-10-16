"""Unit tests for the yt-dlp video download service."""

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.exceptions import ExternalServiceError, VideoDownloadUnavailableError
from app.services import video_download

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42fake-video-payload"


def _fake_mkdtemp_at(directory: Path) -> Callable[[str], str]:
    """Stand-in for tempfile.mkdtemp that uses a known directory."""

    def mkdtemp(prefix: str) -> str:
        directory.mkdir()
        return str(directory)

    return mkdtemp


def _succeeding_run(returncode: int = 0) -> Callable[[list[str]], object]:
    """Stand-in for yt-dlp that writes the destination file it was given."""

    async def run(command: list[str]) -> tuple[int, bytes]:
        Path(command[command.index("-o") + 1]).write_bytes(MP4_BYTES)
        return returncode, b""

    return run


class TestMissingTool:
    def test_names_yt_dlp_when_the_module_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_download.importlib.util, "find_spec", lambda name: None)
        assert video_download._missing_tool() == "yt_dlp"

    def test_names_ffmpeg_when_the_binary_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_download.shutil, "which", lambda name: None)
        assert video_download._missing_tool() == "ffmpeg"

    def test_reports_nothing_when_both_are_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_download.importlib.util, "find_spec", lambda name: object())
        monkeypatch.setattr(video_download.shutil, "which", lambda name: "/usr/bin/ffmpeg")
        assert video_download._missing_tool() is None


class TestBuildCommand:
    def test_keeps_the_documented_format_choice(self, tmp_path: Path) -> None:
        destination = tmp_path / "video.mp4"
        command = video_download._build_command("m5IWI6xFRms", destination)

        assert (
            command[command.index("-f") + 1] == "bv*[vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
        )
        assert command[command.index("--merge-output-format") + 1] == "mp4"
        assert "-o" in command
        assert command[command.index("-o") + 1] == str(destination)
        assert "--download-sections" not in command

    def test_adds_the_section_flag_when_a_range_is_given(self, tmp_path: Path) -> None:
        destination = tmp_path / "video.mp4"
        command = video_download._build_command("m5IWI6xFRms", destination, "*30-90")

        assert command[command.index("--download-sections") + 1] == "*30-90"

    def test_normalizes_the_url_and_refuses_playlists(self, tmp_path: Path) -> None:
        command = video_download._build_command("m5IWI6xFRms&list=x", tmp_path / "video.mp4")

        assert "--no-playlist" in command
        assert command[-1] == "https://www.youtube.com/watch?v=m5IWI6xFRms&list=x"

    def test_caps_the_format_when_a_height_is_given(self, tmp_path: Path) -> None:
        command = video_download._build_command("m5IWI6xFRms", tmp_path / "video.mp4", None, 720)

        assert command[command.index("-f") + 1] == video_download._format_selector(720)


class TestFormatSection:
    def test_formats_both_bounds(self) -> None:
        assert video_download._format_section(30, 90) == "*30-90"

    def test_leaves_the_end_open_when_omitted(self) -> None:
        assert video_download._format_section(30, None) == "*30-inf"


class TestFormatSelector:
    def test_keeps_the_full_ladder_without_a_height(self) -> None:
        assert video_download._format_selector(None) == video_download.FORMAT_SELECTOR

    def test_caps_every_step_at_the_requested_height(self) -> None:
        selector = video_download._format_selector(720)

        assert selector == (
            "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]" "/b[height<=720][ext=mp4]" "/bv*+ba/b"
        )


class TestRunYtDlp:
    async def test_kills_the_process_when_the_download_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class SlowProcess:
            returncode = None

            def __init__(self) -> None:
                self.killed = False
                self.waited = False

            async def communicate(self) -> tuple[bytes, bytes]:
                await asyncio.sleep(5)
                return b"", b""

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> None:
                self.waited = True

        process = SlowProcess()

        async def spawn(*args: object, **kwargs: object) -> SlowProcess:
            return process

        monkeypatch.setattr(video_download.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(video_download, "DOWNLOAD_TIMEOUT_SECONDS", 0.01)

        returncode, _ = await video_download._run_yt_dlp(["yt-dlp"])

        assert returncode == video_download._TIMEOUT_EXIT_CODE
        assert process.killed
        assert process.waited


class TestDownloadVideoFile:
    async def test_returns_the_merged_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(video_download, "_missing_tool", lambda: None)
        monkeypatch.setattr(video_download, "_run_yt_dlp", _succeeding_run())

        path = await video_download.download_video_file("abc123")

        try:
            assert path.name == "video.mp4"
            assert path.read_bytes() == MP4_BYTES
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    async def test_refuses_when_tooling_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_download, "_missing_tool", lambda: "ffmpeg")

        with pytest.raises(VideoDownloadUnavailableError) as excinfo:
            await video_download.download_video_file("abc123")

        assert excinfo.value.code == "SERVICE_UNAVAILABLE"

    async def test_discards_the_directory_when_the_download_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        directory = tmp_path / "download"

        async def failing_run(command: list[str]) -> tuple[int, bytes]:
            return 1, b"ERROR: Video unavailable"

        monkeypatch.setattr(video_download, "_missing_tool", lambda: None)
        monkeypatch.setattr(video_download, "_run_yt_dlp", failing_run)
        monkeypatch.setattr(video_download, "_RETRY_DELAY_SECONDS", 0)
        monkeypatch.setattr(video_download.tempfile, "mkdtemp", _fake_mkdtemp_at(directory))

        with pytest.raises(ExternalServiceError):
            await video_download.download_video_file("abc123")

        assert not directory.exists()

    async def test_sends_the_requested_section_to_yt_dlp(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        directory = tmp_path / "download"
        calls: list[list[str]] = []

        async def recording_run(command: list[str]) -> tuple[int, bytes]:
            calls.append(command)
            Path(command[command.index("-o") + 1]).write_bytes(MP4_BYTES)
            return 0, b""

        monkeypatch.setattr(video_download, "_missing_tool", lambda: None)
        monkeypatch.setattr(video_download, "_run_yt_dlp", recording_run)
        monkeypatch.setattr(video_download.tempfile, "mkdtemp", _fake_mkdtemp_at(directory))

        path = await video_download.download_video_file("abc123", start_seconds=30, end_seconds=90)

        try:
            assert calls[0][calls[0].index("--download-sections") + 1] == "*30-90"
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    async def test_retries_once_when_the_first_attempt_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        directory = tmp_path / "download"
        calls: list[list[str]] = []
        stray_seen: list[bool] = []

        async def flaky_run(command: list[str]) -> tuple[int, bytes]:
            calls.append(command)
            destination = Path(command[command.index("-o") + 1])
            stray = destination.parent / "video.f137.mp4"
            if len(calls) == 1:
                stray.write_bytes(b"partial")
                return 1, b"ERROR: unable to download video data: HTTP Error 403: Forbidden"
            stray_seen.append(stray.exists())
            destination.write_bytes(MP4_BYTES)
            return 0, b""

        monkeypatch.setattr(video_download, "_missing_tool", lambda: None)
        monkeypatch.setattr(video_download, "_run_yt_dlp", flaky_run)
        monkeypatch.setattr(video_download, "_RETRY_DELAY_SECONDS", 0)
        monkeypatch.setattr(video_download.tempfile, "mkdtemp", _fake_mkdtemp_at(directory))

        path = await video_download.download_video_file("abc123")

        try:
            assert len(calls) == 2
            # The half-downloaded stream must not survive into the retry.
            assert stray_seen == [False]
            assert path.read_bytes() == MP4_BYTES
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    async def test_does_not_retry_after_a_slow_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        directory = tmp_path / "download"
        calls: list[list[str]] = []

        async def failing_run(command: list[str]) -> tuple[int, bytes]:
            calls.append(command)
            return 1, b"ERROR: unable to download video data: HTTP Error 403: Forbidden"

        monkeypatch.setattr(video_download, "_missing_tool", lambda: None)
        monkeypatch.setattr(video_download, "_run_yt_dlp", failing_run)
        monkeypatch.setattr(video_download, "_RETRY_BELOW_SECONDS", -1)
        monkeypatch.setattr(video_download.tempfile, "mkdtemp", _fake_mkdtemp_at(directory))

        with pytest.raises(ExternalServiceError):
            await video_download.download_video_file("abc123")

        assert len(calls) == 1

    async def test_discards_the_directory_when_no_file_was_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        directory = tmp_path / "download"

        async def quiet_run(command: list[str]) -> tuple[int, bytes]:
            return 0, b""

        monkeypatch.setattr(video_download, "_missing_tool", lambda: None)
        monkeypatch.setattr(video_download, "_run_yt_dlp", quiet_run)
        monkeypatch.setattr(video_download, "_RETRY_DELAY_SECONDS", 0)
        monkeypatch.setattr(video_download.tempfile, "mkdtemp", _fake_mkdtemp_at(directory))

        with pytest.raises(ExternalServiceError):
            await video_download.download_video_file("abc123")

        assert not directory.exists()

    async def test_caps_the_download_at_the_requested_height(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        directory = tmp_path / "download"
        calls: list[list[str]] = []

        async def recording_run(command: list[str]) -> tuple[int, bytes]:
            calls.append(command)
            Path(command[command.index("-o") + 1]).write_bytes(MP4_BYTES)
            return 0, b""

        monkeypatch.setattr(video_download, "_missing_tool", lambda: None)
        monkeypatch.setattr(video_download, "_run_yt_dlp", recording_run)
        monkeypatch.setattr(video_download.tempfile, "mkdtemp", _fake_mkdtemp_at(directory))

        path = await video_download.download_video_file("abc123", height=720)

        try:
            assert calls[0][calls[0].index("-f") + 1] == video_download._format_selector(720)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)


def _probe_payload(formats: list[object]) -> bytes:
    """Build a yt-dlp metadata payload carrying the given formats."""
    return json.dumps({"formats": formats}).encode()


class TestHeightsFromInfo:
    def test_collects_distinct_avc1_heights_tallest_first(self) -> None:
        payload = _probe_payload(
            [
                {"vcodec": "avc1.640028", "height": 720},
                {"vcodec": "vp09.00.10.08", "height": 1440},
                {"vcodec": "avc1.640032", "height": 1080},
                {"vcodec": "avc1.64001f", "height": 720},
                {"vcodec": "avc1.4d0032", "height": 2160},
                {"vcodec": "avc1.64000d", "height": 96},
                {"vcodec": "avc1.64001e"},
                {"vcodec": None, "height": 480},
                "junk",
            ]
        )

        assert video_download._heights_from_info(payload) == [1080, 720]

    def test_returns_nothing_for_unreadable_json(self) -> None:
        assert video_download._heights_from_info(b"not json") == []

    def test_returns_nothing_when_formats_are_absent(self) -> None:
        assert video_download._heights_from_info(b'{"title": "x"}') == []


class TestBuildProbeCommand:
    def test_dumps_metadata_without_media(self) -> None:
        command = video_download._build_probe_command("m5IWI6xFRms")

        assert "--dump-single-json" in command
        assert "--no-playlist" in command
        assert command[-1] == "https://www.youtube.com/watch?v=m5IWI6xFRms"


class TestRunProbe:
    async def test_kills_the_process_when_the_probe_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class SlowProcess:
            returncode = None

            def __init__(self) -> None:
                self.killed = False
                self.waited = False

            async def communicate(self) -> tuple[bytes, bytes]:
                await asyncio.sleep(5)
                return b"", b""

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> None:
                self.waited = True

        process = SlowProcess()

        async def spawn(*args: object, **kwargs: object) -> SlowProcess:
            return process

        monkeypatch.setattr(video_download.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(video_download, "PROBE_TIMEOUT_SECONDS", 0.01)

        returncode, stdout, _ = await video_download._run_probe(["yt-dlp"])

        assert returncode == video_download._TIMEOUT_EXIT_CODE
        assert stdout == b""
        assert process.killed
        assert process.waited


class TestListVideoHeights:
    async def test_probes_metadata_and_returns_the_ladder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        async def recording_probe(command: list[str]) -> tuple[int, bytes, bytes]:
            calls.append(command)
            return 0, _probe_payload([{"vcodec": "avc1.640032", "height": 1080}]), b""

        monkeypatch.setattr(video_download, "_missing_tool", lambda: None)
        monkeypatch.setattr(video_download, "_run_probe", recording_probe)

        heights = await video_download.list_video_heights("abc123")

        assert heights == [1080]
        assert "--dump-single-json" in calls[0]

    async def test_refuses_when_tooling_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_download, "_missing_tool", lambda: "yt_dlp")

        with pytest.raises(VideoDownloadUnavailableError):
            await video_download.list_video_heights("abc123")

    async def test_raises_when_the_probe_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def failing_probe(command: list[str]) -> tuple[int, bytes, bytes]:
            return 1, b"", b"ERROR: Video unavailable"

        monkeypatch.setattr(video_download, "_missing_tool", lambda: None)
        monkeypatch.setattr(video_download, "_run_probe", failing_probe)

        with pytest.raises(ExternalServiceError):
            await video_download.list_video_heights("abc123")
