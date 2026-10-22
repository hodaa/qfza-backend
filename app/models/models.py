"""SQLAlchemy models for the application."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Video(Base):
    """Video model representing a searchable video from a supported source."""

    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Canonical URL for the source: a youtube.com/watch URL or a Drive file URL.
    video_url: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    # The id the source itself uses: 11 chars on YouTube, 33 on Drive.
    video_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    # The keyword an anonymous visitor last searched in this video. Signed-in
    # searches live in search_history instead (one row per user), so this single
    # slot is the only trace an anonymous search leaves.
    last_keyword: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Downloadable H.264 heights the worker last probed for this video, tallest
    # first. Null until a probe has run; an empty list is a real answer (no
    # named H.264 quality) and keeps the picker from re-probing on every view.
    available_heights: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    # When the heights above were probed. Drives the staleness check that
    # re-probes in the background on a much later view.
    formats_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_tsvector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    transcribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Relationships
    transcript_words: Mapped[list["TranscriptWord"]] = relationship(
        "TranscriptWord",
        back_populates="video",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    jobs: Mapped[list["Job"]] = relationship(
        "Job",
        back_populates="video",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("idx_videos_transcript_tsvector", "transcript_tsvector", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<Video(id={self.id}, video_id={self.video_id}, title={self.title})>"


class TranscriptWord(Base):
    """Transcript word model for exact phrase search."""

    __tablename__ = "transcript_words"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
    )
    word_index: Mapped[int] = mapped_column(Integer, nullable=False)
    word: Mapped[str] = mapped_column(String(500), nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)

    # Relationships
    video: Mapped["Video"] = relationship("Video", back_populates="transcript_words")

    __table_args__ = (
        Index("idx_transcript_words_video_word", "video_id", "word"),
        UniqueConstraint("video_id", "word_index", name="uq_transcript_words_video_index"),
    )

    def __repr__(self) -> str:
        return (
            f"<TranscriptWord(video_id={self.video_id}, word={self.word}, index={self.word_index})>"
        )


class ContactMessage(Base):
    """Contact form message sent from the public website."""

    __tablename__ = "contact_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (Index("idx_contact_messages_created_at", "created_at"),)

    def __repr__(self) -> str:
        return f"<ContactMessage(id={self.id}, email={self.email})>"


class Job(Base):
    """Job model for tracking transcription jobs."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    video: Mapped["Video"] = relationship("Video", back_populates="jobs")

    __table_args__ = (
        # Unique partial index: only one in-flight job per video
        Index(
            "uq_jobs_video_in_flight",
            "video_id",
            unique=True,
            postgresql_where="status IN ('pending', 'processing')",
        ),
        Index("idx_jobs_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<Job(id={self.id}, video_id={self.video_id}, status={self.status})>"


class VideoDownload(Base):
    """One offloaded video download tracked across the worker round trip.

    The backend never touches the media: it queues the request, the worker
    produces the MP4 and uploads it to blob storage, and this row carries the
    blob URL to whichever client polls it. Lives through five states:

    * ``pending`` - queued, the worker has not picked it up
    * ``processing`` - the worker is downloading/merging/uploading
    * ``ready`` - the blob URL is set and fetchable
    * ``failed`` - the worker reported an error (``error`` holds the message)
    * ``consumed`` - the client downloaded it; the blob was deleted

    ``start_seconds``/``end_seconds``/``height`` mirror what the client asked
    for (null = whole video / best quality). The row outlives the blob so a
    late poll gets an honest "consumed" rather than a 404; a retention sweep
    removes old rows and any blob they still point at.
    """

    __tablename__ = "video_downloads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    start_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blob_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_video_downloads_video_id", "video_id"),
        Index("idx_video_downloads_status_updated", "status", "updated_at"),
    )

    def __repr__(self) -> str:
        return f"<VideoDownload(id={self.id}, video_id={self.video_id}, status={self.status})>"


class User(Base):
    """A registered account. Anonymous visitors never have a row here."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    # Nullable: a Google-only account has no password.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    # The name Google returns on sign-in, refreshed from it each time. Nullable:
    # a password-only account has none, and so does a Google account until its
    # next sign-in. Greeting falls back to the email when this is null.
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Lockout state. Resets on a successful login or an admin unlock.
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failed_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email={self.email})>"


class Session(Base):
    """A login session. Only the token hash is stored, never the token."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user: Mapped["User"] = relationship("User")

    __table_args__ = (
        Index("idx_sessions_user_id", "user_id"),
        Index("idx_sessions_expires_at", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<Session(id={self.id}, user_id={self.user_id}, expires_at={self.expires_at})>"


class SearchHistory(Base):
    """One search by a registered user. Anonymous searches are never recorded."""

    __tablename__ = "search_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 50 chars: wide enough for a Drive file id (33) as well as a YouTube one.
    video_id: Mapped[str] = mapped_column(String(50), nullable=False)
    keyword: Mapped[str] = mapped_column(String(500), nullable=False)
    locale: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    # Where the first match sat when the search ran, so the entry can be replayed
    # at the moment it was found rather than from the top of the video. Null
    # until a transcript exists, and on every row written before column 005.
    progress_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # All match positions (seconds) when the search ran, so the history page can
    # show every moment the keyword was found. Null on rows written before this
    # column existed.
    match_timestamps: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    # Every result the search returned, snapshotted whole: timestamp, position and
    # the snippet around the phrase. match_timestamps only says *where* the matches
    # sat, which is enough to seek but not to show what was found; this is what the
    # history page replays. Null on rows written before this column existed and on
    # any row whose transcript has not resolved yet.
    match_results: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)
    # Snapshotted at search time on purpose. The videos row is the live truth
    # and can be refetched or renamed; this is what the video was called when the
    # visitor searched it, which is what they will recognise.
    video_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    user: Mapped["User"] = relationship("User")

    __table_args__ = (Index("idx_search_history_user_created", "user_id", "created_at"),)

    def __repr__(self) -> str:
        return f"<SearchHistory(id={self.id}, user_id={self.user_id}, video_id={self.video_id})>"


class AuthToken(Base):
    """A single-use token: email verification or password reset.

    Storing only the hash means a database dump does not hand over working links.
    """

    __tablename__ = "auth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    user: Mapped["User"] = relationship("User")

    def __repr__(self) -> str:
        return f"<AuthToken(id={self.id}, purpose={self.purpose}, used={self.used_at is not None})>"


class LoginAttempt(Base):
    """A rolling failed-login counter for one client address.

    Exists so an attacker cannot grind the three-strike lockout against many
    accounts from a single host. One row per address per hour window.
    """

    __tablename__ = "login_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("ip", "window_started_at", name="uq_login_attempts_ip_window"),
    )

    def __repr__(self) -> str:
        return f"<LoginAttempt(ip={self.ip}, count={self.failed_count})>"
