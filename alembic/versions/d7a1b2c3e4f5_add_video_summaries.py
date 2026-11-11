"""add video_summaries table

Highlight videos are rendered off-box by the worker: the backend queues a
``video_summaries`` row, the worker scores the transcript, cuts and concatenates
the selected moments, uploads the MP4 to blob storage, and reports its URL. The
ready row caches that URL so repeat clicks open the same render instead of
paying for another pass.

Revision ID: d7a1b2c3e4f5
Revises: c2f5d8e1a7b4
Create Date: 2026-11-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d7a1b2c3e4f5"
down_revision: str | None = "c2f5d8e1a7b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the video-summary request ledger."""
    op.create_table(
        "video_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("blob_url", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_video_summaries_video_id", "video_summaries", ["video_id"])
    op.create_index(
        "idx_video_summaries_status_updated", "video_summaries", ["status", "updated_at"]
    )
    op.create_index(
        "uq_video_summaries_video_in_flight",
        "video_summaries",
        ["video_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )


def downgrade() -> None:
    """Drop the video-summary request ledger."""
    op.drop_index("uq_video_summaries_video_in_flight", table_name="video_summaries")
    op.drop_index("idx_video_summaries_status_updated", table_name="video_summaries")
    op.drop_index("idx_video_summaries_video_id", table_name="video_summaries")
    op.drop_table("video_summaries")
