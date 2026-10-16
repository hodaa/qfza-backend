"""Unit tests for the Celery messaging client."""

from uuid import UUID

from app.services import messaging


class TestDispatchTranscription:
    """Tests for publishing transcription jobs to the worker queue."""

    def test_sends_worker_task_with_job_id(self, monkeypatch) -> None:
        sent = []
        monkeypatch.setattr(
            messaging.celery_app,
            "send_task",
            lambda name, args=None, **kwargs: sent.append((name, args)),
        )

        messaging.dispatch_transcription(UUID("12345678-1234-5678-1234-567812345678"))

        assert sent == [
            (
                "app.tasks.transcription.download_and_transcribe",
                ["12345678-1234-5678-1234-567812345678"],
            )
        ]


class TestDispatchResumeTranscription:
    """Tests for publishing Assembly resume jobs to the worker queue."""

    def test_sends_worker_task_with_resume_kwargs(self, monkeypatch) -> None:
        sent = []
        monkeypatch.setattr(
            messaging.celery_app,
            "send_task",
            lambda name, args=None, kwargs=None: sent.append((name, args, kwargs)),
        )

        messaging.dispatch_resume_transcription(
            UUID("12345678-1234-5678-1234-567812345678"),
            resume_token="asm-123",
            resume_provider="yt-dlp",
        )

        assert sent == [
            (
                "app.tasks.transcription.download_and_transcribe",
                ["12345678-1234-5678-1234-567812345678"],
                {"resume_token": "asm-123", "resume_provider": "yt-dlp"},
            )
        ]


class TestDispatchVideoDownload:
    """Tests for publishing offloaded video downloads to the worker queue."""

    def test_sends_worker_task_with_download_id(self, monkeypatch) -> None:
        sent = []
        monkeypatch.setattr(
            messaging.celery_app,
            "send_task",
            lambda name, args=None, **kwargs: sent.append((name, args)),
        )

        messaging.dispatch_video_download(UUID("12345678-1234-5678-1234-567812345678"))

        assert sent == [
            (
                "app.tasks.downloads.download_video",
                ["12345678-1234-5678-1234-567812345678"],
            )
        ]


class TestDispatchFormatsProbe:
    """Tests for publishing format probes to the worker queue."""

    def test_sends_worker_task_with_video_id_and_url(self, monkeypatch) -> None:
        sent = []
        monkeypatch.setattr(
            messaging.celery_app,
            "send_task",
            lambda name, args=None, **kwargs: sent.append((name, args)),
        )

        messaging.dispatch_formats_probe(
            "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

        assert sent == [
            (
                "app.tasks.formats.probe_video_formats",
                ["dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
            )
        ]


class TestCeleryAppConfiguration:
    """Tests for the Celery app configuration."""

    def test_uses_json_serialization(self) -> None:
        assert messaging.celery_app.conf.task_serializer == "json"
        assert messaging.celery_app.conf.accept_content == ["json"]
        assert messaging.celery_app.conf.result_serializer == "json"

    def test_tracks_no_result_backend(self) -> None:
        assert messaging.celery_app.conf.result_backend is None
