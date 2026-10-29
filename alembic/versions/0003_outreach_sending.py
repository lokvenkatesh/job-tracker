"""outreach sending: send queue, idempotent sends, reply tracking, follow-ups

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models import UTCDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("outreach_campaigns") as t:
        t.add_column(sa.Column("send_requested_at", UTCDateTime()))
    with op.batch_alter_table("outreach_emails") as t:
        t.add_column(
            sa.Column("allow_recent", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        t.add_column(sa.Column("followup_of_id", sa.Integer()))
        t.add_column(sa.Column("message_id_header", sa.String(255)))
        t.add_column(sa.Column("send_attempts", sa.Integer(), nullable=False, server_default="0"))
        t.add_column(sa.Column("send_error", sa.String(300)))
        t.add_column(sa.Column("replied_at", UTCDateTime()))
        t.create_foreign_key(
            "fk_outreach_emails_followup_of_id",
            "outreach_emails",
            ["followup_of_id"],
            ["id"],
            ondelete="SET NULL",
        )
        t.create_index("ix_outreach_emails_followup_of_id", ["followup_of_id"])
    with op.batch_alter_table("sync_state") as t:
        t.add_column(sa.Column("outreach_next_send_at", UTCDateTime()))


def downgrade() -> None:
    with op.batch_alter_table("sync_state") as t:
        t.drop_column("outreach_next_send_at")
    with op.batch_alter_table("outreach_emails") as t:
        t.drop_index("ix_outreach_emails_followup_of_id")
        t.drop_constraint("fk_outreach_emails_followup_of_id", type_="foreignkey")
        for col in (
            "replied_at",
            "send_error",
            "send_attempts",
            "message_id_header",
            "followup_of_id",
            "allow_recent",
        ):
            t.drop_column(col)
    with op.batch_alter_table("outreach_campaigns") as t:
        t.drop_column("send_requested_at")
