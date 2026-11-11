"""Pydantic schemas for request/response validation."""

import re
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, HttpUrl, field_validator

_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

#: Bounds of the H.264 download ladder the API accepts. Below the floor no
#: useful picture remains; above the ceiling YouTube serves only VP9/AV1,
#: which Apple players cannot decode as video.
MIN_VIDEO_HEIGHT = 144
MAX_AVC1_HEIGHT = 1080


class ContactRequest(BaseModel):
    """Request schema for POST /api/contact."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=100, description="Sender display name")
    email: str = Field(..., min_length=1, max_length=254, description="Sender email address")
    message: str = Field(..., min_length=1, max_length=500, description="Message body")

    @field_validator("name", "message")
    @classmethod
    def reject_blank_text(cls, v: str) -> str:
        """Reject text that is only whitespace."""
        if not v.strip():
            raise ValueError("Value cannot be empty or whitespace only")
        return v.strip()

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        """Reject malformed email addresses and normalize to lowercase."""
        if not _EMAIL_PATTERN.fullmatch(v):
            raise ValueError("Email must be a valid address")
        return v.lower()


class ContactResponse(BaseModel):
    """Response schema for POST /api/contact."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class JobStatus(str, Enum):
    """Job status enumeration."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class SearchStatus(str, Enum):
    """Search result status enumeration."""

    FOUND = "found"
    PROCESSING = "processing"
    NOT_FOUND = "not_found"


class TimestampResult(BaseModel):
    """Single timestamp result for a keyword match."""

    model_config = ConfigDict(from_attributes=True)

    timestamp: str = Field(..., description="Formatted timestamp (MM:SS)")
    progress_seconds: float = Field(..., description="Progress in seconds")
    text_snippet: str | None = Field(None, description="Surrounding text context")


class SearchRequest(BaseModel):
    """Request schema for POST /api/search."""

    model_config = ConfigDict(str_strip_whitespace=True)

    video_url: HttpUrl = Field(
        ...,
        validation_alias=AliasChoices("video_url", "youtube_url"),
        description="Video URL (YouTube or Google Drive)",
    )
    keyword: str = Field(..., min_length=1, description="Keyword or phrase to search")

    @field_validator("keyword")
    @classmethod
    def validate_keyword_not_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Keyword cannot be empty or whitespace only")
        return v.strip()


class VideoResolveRequest(BaseModel):
    """Request schema for POST /api/videos/resolve."""

    model_config = ConfigDict(str_strip_whitespace=True)

    video_url: HttpUrl = Field(
        ...,
        validation_alias=AliasChoices("video_url", "youtube_url"),
        description="Video URL (YouTube or Google Drive)",
    )


class VideoResolveResponse(BaseModel):
    """A stored video identified by a pasted link."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    video_id: str
    title: str | None = None
    duration_seconds: int | None = None
    transcribed: bool = Field(..., description="Whether the transcript is ready")


class VideoPrepareResponse(BaseModel):
    """Whether a video's transcript is ready or a job is running."""

    model_config = ConfigDict(from_attributes=True)

    status: str = Field(..., description="ready | processing")
    video_id: UUID
    job_id: UUID | None = Field(None, description="Set while transcription is running")


class SearchResponseCached(BaseModel):
    """Response schema for cached search results."""

    model_config = ConfigDict(from_attributes=True)

    status: SearchStatus = SearchStatus.FOUND
    results: list[TimestampResult] = Field(default_factory=list)
    no_speech: bool = Field(
        default=False,
        description="Video is transcribed but contains no speech or sound (null transcript)",
    )


class SearchResponseProcessing(BaseModel):
    """Response schema for search that triggered a job."""

    model_config = ConfigDict(from_attributes=True)

    status: SearchStatus = SearchStatus.PROCESSING
    job_id: UUID
    video_id: UUID


class VideoSearchResponse(BaseModel):
    """Response schema for GET /api/video/{video_id}/search."""

    model_config = ConfigDict(from_attributes=True)

    status: SearchStatus = SearchStatus.FOUND
    results: list[TimestampResult] = Field(default_factory=list)
    no_speech: bool = Field(
        default=False,
        description="Video is transcribed but contains no speech or sound (null transcript)",
    )


class VideoSearchResult(BaseModel):
    """A video matching a catalog-wide full-text search."""

    model_config = ConfigDict(from_attributes=True)

    video_id: UUID
    youtube_video_id: str
    youtube_url: str
    title: str | None = Field(None, description="Video title")
    duration_seconds: int | None = Field(None, description="Video duration in seconds")
    snippet: str | None = Field(None, description="ts_headline snippet around the match")
    rank: float = Field(0.0, description="ts_rank relevance score")


class FullTextSearchResponse(BaseModel):
    """Response schema for GET /api/videos/search."""

    model_config = ConfigDict(from_attributes=True)

    status: SearchStatus = SearchStatus.FOUND
    query: str
    results: list[VideoSearchResult] = Field(default_factory=list)


class VideoFormatsResponse(BaseModel):
    """Response schema for GET /api/videos/{youtube_video_id}/formats."""

    model_config = ConfigDict(from_attributes=True)

    heights: list[int] = Field(
        default_factory=list,
        description="Downloadable H.264 heights in pixels, tallest first",
    )


class DownloadCreateRequest(BaseModel):
    """Request schema for POST /api/videos/{youtube_video_id}/downloads."""

    start: float | None = Field(default=None, ge=0, description="Section start in seconds")
    end: float | None = Field(default=None, ge=0, description="Section end in seconds")
    height: int | None = Field(
        default=None,
        ge=MIN_VIDEO_HEIGHT,
        le=MAX_AVC1_HEIGHT,
        description="Preferred H.264 quality in pixels",
    )


class DownloadCreateResponse(BaseModel):
    """Response schema for a queued video download."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str = Field(..., description="Lifecycle state; starts as pending")


class DownloadStatusResponse(BaseModel):
    """Response schema for the download status and consume routes."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str = Field(
        ...,
        description="pending | processing | ready | failed | consumed",
    )
    url: str | None = Field(
        None,
        description="Blob URL of the produced file; set only while ready",
    )
    error: str | None = Field(None, description="User-safe failure message when failed")
    filename: str | None = Field(
        None,
        description="Filename the client should save the file under",
    )


class NotesCreateResponse(BaseModel):
    """Response schema for a queued lecture-notes request."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str = Field(..., description="Lifecycle state; starts as pending")


class NotesStatusResponse(BaseModel):
    """Response schema for the lecture-notes status route."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str = Field(..., description="pending | processing | ready | failed")
    url: str | None = Field(
        None,
        description="Blob URL of the generated PDF; set only while ready",
    )
    error: str | None = Field(None, description="User-safe failure message when failed")
    filename: str | None = Field(
        None,
        description="Filename the client should save the file under",
    )


class StatusResponse(BaseModel):
    """Response schema for GET /api/status/{job_id}."""

    model_config = ConfigDict(from_attributes=True)

    status: JobStatus
    video_id: UUID
    progress: int | None = None
    results: list[TimestampResult] | None = None
    error: str | None = None


class VideoResponse(BaseModel):
    """Video information response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    youtube_url: str
    video_id: str
    title: str | None = None
    duration_seconds: int | None = None
    transcribed_at: datetime | None = None
    created_at: datetime


class JobResponse(BaseModel):
    """Job information response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    video_id: UUID
    status: JobStatus
    progress: int | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ErrorResponse(BaseModel):
    """Error response schema."""

    model_config = ConfigDict(from_attributes=True)

    error: dict[str, Any]


# Union types for OpenAPI documentation
SearchResponse = SearchResponseCached | SearchResponseProcessing
VideoSearchResponseUnion = VideoSearchResponse


class InternalWordData(BaseModel):
    """A single transcript word in an internal store-transcript request."""

    model_config = ConfigDict(str_strip_whitespace=True)

    word_index: int = Field(..., ge=0, description="Zero-based position of the word")
    word: str = Field(..., min_length=1, description="Normalized word text")
    start_time: float = Field(..., ge=0, description="Start time in seconds")
    end_time: float = Field(..., ge=0, description="End time in seconds")


class InternalStoreTranscriptRequest(BaseModel):
    """Request schema for POST /internal/jobs/{job_id}/transcript."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(..., min_length=1, description="Video title")
    duration_seconds: int = Field(..., ge=0, description="Video duration in seconds")
    language: str = Field(default="en", min_length=1, description="Transcript language code")
    transcript_text: str = Field(
        default="",
        description="Full transcript text; empty means the video has no speech",
    )
    provider: str = Field(
        default="", max_length=50, description="Provider that produced the transcript"
    )
    words: list[InternalWordData] = Field(default_factory=list, description="Per-word timestamps")


class InternalFailRequest(BaseModel):
    """Request schema for POST /internal/jobs/{job_id}/fail."""

    model_config = ConfigDict(str_strip_whitespace=True)

    error: str = Field(..., min_length=1, description="User-safe error message")


class InternalProgressRequest(BaseModel):
    """Request schema for POST /internal/jobs/{job_id}/progress."""

    progress: int = Field(..., ge=1, le=99, description="Intermediate progress percent")


class InternalCompleteRequest(BaseModel):
    """Request schema for POST /internal/jobs/{job_id}/complete."""

    model_config = ConfigDict(str_strip_whitespace=True)

    message: str | None = Field(
        default=None,
        max_length=500,
        description="Optional outcome note (e.g. no speech detected) stored on the job",
    )


class InternalJobResponse(BaseModel):
    """Response schema for GET /internal/jobs/{job_id}."""

    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    video_id: UUID
    youtube_video_id: str
    youtube_url: str
    status: str


class InternalStatusResponse(BaseModel):
    """Response schema for internal job lifecycle mutations."""

    model_config = ConfigDict(from_attributes=True)

    status: str


class InternalDownloadResponse(BaseModel):
    """Response schema for GET /internal/downloads/{download_id}."""

    model_config = ConfigDict(from_attributes=True)

    download_id: UUID
    video_id: UUID
    youtube_video_id: str
    youtube_url: str
    status: str
    start_seconds: int | None = None
    end_seconds: int | None = None
    height: int | None = None


class SummaryCreateResponse(BaseModel):
    """Response schema for a queued video-summary request."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str = Field(..., description="Lifecycle state; starts as pending")


class SummaryStatusResponse(BaseModel):
    """Response schema for the video-summary status route."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str = Field(..., description="pending | processing | ready | failed")
    url: str | None = Field(
        None,
        description="Blob URL of the generated MP4; set only while ready",
    )
    error: str | None = Field(None, description="User-safe failure message when failed")
    filename: str | None = Field(
        None,
        description="Filename the client should save the file under",
    )


class InternalNotesLine(BaseModel):
    """One timestamped transcript line sent to the worker."""

    seconds: float = Field(..., ge=0, description="Start position of the line in seconds")
    text: str = Field(..., min_length=1, description="Line text")


class InternalNotesResponse(BaseModel):
    """Response schema for GET /internal/notes/{note_id}."""

    model_config = ConfigDict(from_attributes=True)

    note_id: UUID
    video_id: UUID
    youtube_video_id: str
    youtube_url: str
    title: str
    duration_seconds: int | None = None
    language: str | None = None
    is_rtl: bool = False
    status: str
    transcript_lines: list[InternalNotesLine] = Field(default_factory=list)


class InternalSummaryLine(BaseModel):
    """One timestamped transcript line sent to the summary worker."""

    seconds: float = Field(..., ge=0, description="Start position of the line in seconds")
    text: str = Field(..., min_length=1, description="Line text")


class InternalSummaryResponse(BaseModel):
    """Response schema for GET /internal/summaries/{summary_id}."""

    model_config = ConfigDict(from_attributes=True)

    summary_id: UUID
    video_id: UUID
    youtube_video_id: str
    youtube_url: str
    title: str
    duration_seconds: int | None = None
    language: str | None = None
    target_seconds: int | None = None
    status: str
    transcript_lines: list[InternalSummaryLine] = Field(default_factory=list)


class InternalDownloadReadyRequest(BaseModel):
    """Request schema for POST /internal/downloads/{download_id}/ready."""

    model_config = ConfigDict(str_strip_whitespace=True)

    blob_url: str = Field(..., min_length=1, description="Public blob URL of the produced file")


class InternalDownloadFailRequest(BaseModel):
    """Request schema for POST /internal/downloads/{download_id}/failed."""

    model_config = ConfigDict(str_strip_whitespace=True)

    error: str = Field(..., min_length=1, max_length=500, description="User-safe error message")


class InternalVideoFormatsRequest(BaseModel):
    """Request schema for POST /internal/videos/{youtube_video_id}/formats."""

    heights: list[int] = Field(
        default_factory=list,
        description="Distinct H.264 heights found, tallest first; empty is a real answer",
    )


class AssemblyWebhookStatus(str, Enum):
    """Assembly.ai webhook status values."""

    COMPLETED = "completed"
    ERROR = "error"


class BenchmarkBackendInfo(BaseModel):
    """Backend-side context for a database benchmark."""

    timestamp: datetime
    region: str | None = None
    platform: str | None = None


class BenchmarkDatabaseInfo(BaseModel):
    """Database-side context for a database benchmark."""

    timestamp: datetime
    server_version: str
    host: str | None = None
    name: str | None = None


class BenchmarkLatency(BaseModel):
    """Connection and query round-trip timings in milliseconds."""

    connect_ms: float
    first_query_ms: float
    min_ms: float
    median_ms: float
    mean_ms: float
    p95_ms: float
    samples_ms: list[float]


class BenchmarkResponse(BaseModel):
    """Response schema for GET /internal/benchmark/db."""

    backend: BenchmarkBackendInfo
    database: BenchmarkDatabaseInfo
    latency: BenchmarkLatency
    samples: int


class AssemblyWebhookRequest(BaseModel):
    """Completion callback payload sent by Assembly.ai."""

    model_config = ConfigDict(extra="ignore")

    transcript_id: str = Field(..., min_length=1, description="Assembly transcript id")
    status: AssemblyWebhookStatus = Field(..., description="Assembly job status")


class WebhookAckResponse(BaseModel):
    """Acknowledgment returned to provider completion webhooks."""

    status: str = Field(..., description="Ack status (ok)")
