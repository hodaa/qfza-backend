"""add video_notes table

Lecture notes are generated off-box by the worker: the backend queues a
``video_notes`` row, the worker reads the lecture's slides and transcript,
renders a PDF, and uploads it to blob storage. The ready row caches the blob
URL so repeat clicks open the same document instead of paying for OCR again.

Revision ID: c2f5d8e1a7b4
Revises: b7e41c9a2f63
Create Date: 2026-10-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c2f5d8e1a7b4"
down_revision: str | None = "b7e41c9a2f63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the lecture-notes request ledger."""
    op.create_table(
        "video_notes",
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
    op.create_index("idx_video_notes_video_id", "video_notes", ["video_id"])
    op.create_index("idx_video_notes_status_updated", "video_notes", ["status", "updated_at"])
    op.create_index(
        "uq_video_notes_video_in_flight",
        "video_notes",
        ["video_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )


def downgrade() -> None:
    """Drop the lecture-notes request ledger."""
    op.drop_index("uq_video_notes_video_in_flight", table_name="video_notes")
    op.drop_index("idx_video_notes_status_updated", table_name="video_notes")
    op.drop_index("idx_video_notes_video_id", table_name="video_notes")
    op.drop_table("video_notes")
