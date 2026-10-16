"""add video_downloads table and cached format heights on videos

Downloads move off the backend and onto the worker: the backend queues a
``video_downloads`` row, the worker produces the MP4, uploads it to blob
storage, and reports the blob URL back. The row carries that URL to the polling
client and lives past the blob (a late poll gets "consumed", not a 404); a
retention sweep removes old rows and any blob they still point at.

``videos.available_heights`` caches the H.264 quality ladder the worker probed,
so listing formats becomes a database read instead of a live YouTube call from
the serverless runtime (which YouTube bot-checks). Null means no probe has run
yet; an empty list is a real answer and is not re-probed on every view.

Revision ID: 3f8c2a71d9b4
Revises: 0026b8a1d4e77
Create Date: 2026-10-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3f8c2a71d9b4"
down_revision: str | None = "0026b8a1d4e77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the download ledger and the formats cache columns."""
    op.create_table(
        "video_downloads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("start_seconds", sa.Integer(), nullable=True),
        sa.Column("end_seconds", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("blob_url", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_video_downloads_video_id", "video_downloads", ["video_id"])
    op.create_index(
        "idx_video_downloads_status_updated", "video_downloads", ["status", "updated_at"]
    )
    op.add_column(
        "videos",
        sa.Column("available_heights", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "videos",
        sa.Column("formats_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the formats cache columns and the download ledger."""
    op.drop_column("videos", "formats_refreshed_at")
    op.drop_column("videos", "available_heights")
    op.drop_index("idx_video_downloads_status_updated", table_name="video_downloads")
    op.drop_index("idx_video_downloads_video_id", table_name="video_downloads")
    op.drop_table("video_downloads")
