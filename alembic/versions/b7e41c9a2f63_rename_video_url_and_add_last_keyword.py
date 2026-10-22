"""rename videos.youtube_url to video_url, add videos.last_keyword, widen search_history.video_id

Videos are no longer only YouTube videos: a row can now point at a Google Drive
file, so the canonical URL column drops the platform from its name. The rename
keeps the data (and the unique constraint's behaviour; the constraint itself is
renamed to match).

``videos.last_keyword`` stores the keyword an anonymous visitor last searched in
that video. Signed-in searches are already filed per user in ``search_history``;
this single slot is the only trace an anonymous search leaves, so it does not
need a user id or an index.

``search_history.video_id`` widens from 32 to 50 characters so a signed-in
submitted Drive file id (33 chars) fits. Downgrading truncates ids longer than
32 characters.

Revision ID: b7e41c9a2f63
Revises: 3f8c2a71d9b4
Create Date: 2026-10-19

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e41c9a2f63"
down_revision: str | None = "3f8c2a71d9b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename the URL column, add the anonymous keyword slot, widen the id."""
    op.alter_column("videos", "youtube_url", new_column_name="video_url")
    op.execute(
        "ALTER TABLE videos RENAME CONSTRAINT videos_youtube_url_key TO videos_video_url_key"
    )
    op.add_column("videos", sa.Column("last_keyword", sa.String(500), nullable=True))
    op.alter_column(
        "search_history",
        "video_id",
        type_=sa.String(50),
        existing_type=sa.String(32),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Reverse the rename, the keyword slot and the id widening."""
    op.alter_column(
        "search_history",
        "video_id",
        type_=sa.String(32),
        existing_type=sa.String(50),
        existing_nullable=False,
    )
    op.drop_column("videos", "last_keyword")
    op.execute(
        "ALTER TABLE videos RENAME CONSTRAINT videos_video_url_key TO videos_youtube_url_key"
    )
    op.alter_column("videos", "video_url", new_column_name="youtube_url")
