"""multiple inboxes: mail_accounts with encrypted tokens and per-inbox sync cursor

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models import UTCDateTime

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("readonly_token", sa.Text()),
        sa.Column("send_token", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("initial_backfill_days", sa.Integer(), nullable=False),
        sa.Column("last_history_id", sa.String(32)),
        sa.Column("last_run_at", UTCDateTime()),
        sa.Column("last_error", sa.String(300)),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    for table, fk in (
        ("emails", "fk_emails_mail_account_id"),
        ("outreach_campaigns", "fk_outreach_campaigns_mail_account_id"),
        ("outreach_emails", "fk_outreach_emails_mail_account_id"),
    ):
        with op.batch_alter_table(table) as t:
            t.add_column(sa.Column("mail_account_id", sa.Integer()))
            t.create_foreign_key(
                fk, "mail_accounts", ["mail_account_id"], ["id"], ondelete="SET NULL"
            )
    with op.batch_alter_table("emails") as t:
        t.create_index("ix_emails_mail_account_id", ["mail_account_id"])


def downgrade() -> None:
    with op.batch_alter_table("emails") as t:
        t.drop_index("ix_emails_mail_account_id")
    for table, fk in (
        ("outreach_emails", "fk_outreach_emails_mail_account_id"),
        ("outreach_campaigns", "fk_outreach_campaigns_mail_account_id"),
        ("emails", "fk_emails_mail_account_id"),
    ):
        with op.batch_alter_table(table) as t:
            t.drop_constraint(fk, type_="foreignkey")
            t.drop_column("mail_account_id")
    op.drop_table("mail_accounts")
