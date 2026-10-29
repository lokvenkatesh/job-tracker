"""outreach drafts: resume file, campaign brief, draft issues and skip reasons

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models import UTCDateTime

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("resumes") as t:
        t.add_column(sa.Column("content", sa.LargeBinary()))
        t.add_column(sa.Column("content_type", sa.String(100)))
    with op.batch_alter_table("outreach_campaigns") as t:
        t.add_column(sa.Column("brief", sa.JSON()))
    with op.batch_alter_table("outreach_emails") as t:
        t.add_column(sa.Column("skip_reason", sa.String(100)))
        t.add_column(sa.Column("issues", sa.JSON()))
        t.add_column(sa.Column("generated_at", UTCDateTime()))


def downgrade() -> None:
    with op.batch_alter_table("outreach_emails") as t:
        t.drop_column("generated_at")
        t.drop_column("issues")
        t.drop_column("skip_reason")
    with op.batch_alter_table("outreach_campaigns") as t:
        t.drop_column("brief")
    with op.batch_alter_table("resumes") as t:
        t.drop_column("content_type")
        t.drop_column("content")
