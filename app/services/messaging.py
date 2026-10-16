"""Messaging client for publishing transcription jobs to the worker queue."""

import ssl
from uuid import UUID

from celery import Celery

from app.core.config import get_settings

_WORKER_TASK_NAME = "app.tasks.transcription.download_and_transcribe"
_DOWNLOAD_TASK_NAME = "app.tasks.downloads.download_video"
_FORMATS_TASK_NAME = "app.tasks.formats.probe_video_formats"

_settings = get_settings()

broker_url = _settings.broker_url

# Redis over TLS: kombu reads the query param and needs broker_use_ssl too.
if broker_url.startswith("rediss://"):
    separator = "&" if "?" in broker_url else "?"
    broker_url = f"{broker_url}{separator}ssl_cert_reqs=CERT_REQUIRED"

celery_app = Celery("jumpto", broker=broker_url)

if broker_url.startswith("rediss://"):
    celery_app.conf.broker_use_ssl = {
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }
    celery_app.conf.result_backend_transport_options = {
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }
elif broker_url.startswith("amqps://"):
    # The amqp/pyamqp transport expects amqp-style ssl options (cert_reqs,
    # not redis's ssl_cert_reqs) and loads the system CA store when none is
    # given, so a plain cert_reqs verifies against trusted CAs.
    celery_app.conf.broker_use_ssl = {
        "cert_reqs": ssl.CERT_REQUIRED,
    }

celery_app.conf.broker_connection_retry_on_startup = True

celery_app.conf.result_backend = None

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


def dispatch_transcription(job_id: UUID) -> None:
    """Publish a transcription job to the worker's queue."""
    celery_app.send_task(_WORKER_TASK_NAME, args=[str(job_id)])


def dispatch_resume_transcription(job_id: UUID, resume_token: str, resume_provider: str) -> None:
    """Publish a resume of an Assembly.ai job to the worker's queue.

    The worker task re-runs ``download_and_transcribe`` with a ``resume_token``
    so it polls the Assembly.ai transcript instead of re-downloading audio.
    """
    celery_app.send_task(
        _WORKER_TASK_NAME,
        args=[str(job_id)],
        kwargs={
            "resume_token": resume_token,
            "resume_provider": resume_provider,
        },
    )


def dispatch_video_download(download_id: UUID) -> None:
    """Publish a video download request to the worker's queue."""
    celery_app.send_task(_DOWNLOAD_TASK_NAME, args=[str(download_id)])


def dispatch_formats_probe(youtube_video_id: str, youtube_url: str) -> None:
    """Publish a format probe for one video to the worker's queue."""
    celery_app.send_task(_FORMATS_TASK_NAME, args=[youtube_video_id, youtube_url])
