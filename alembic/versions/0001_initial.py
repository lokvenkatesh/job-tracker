"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models import Embedding, UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "status IN ('applied', 'assessment', 'interview', 'offer', 'rejected')"
OUTREACH_STATUSES = "status IN ('draft', 'approved', 'sent', 'skipped', 'replied')"


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company", sa.String(200), nullable=False),
        sa.Column("role", sa.String(300)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("applied_date", sa.Date()),
        sa.Column("last_update", UTCDateTime(), nullable=False),
        sa.Column("status_changed_at", UTCDateTime(), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(STATUSES, name="ck_applications_status"),
    )

    op.create_table(
        "emails",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gmail_id", sa.String(64), nullable=False, unique=True),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="SET NULL"),
        ),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("received_at", UTCDateTime(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sender_email", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(1000), nullable=False),
        sa.Column("company", sa.String(200)),
        sa.Column("role", sa.String(300)),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("review_reason", sa.String(100)),
        sa.Column("embedding", Embedding()),
        sa.Column("processed_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_emails_thread_id", "emails", ["thread_id"])
    op.create_index("ix_emails_application_id", "emails", ["application_id"])
    op.create_index("ix_emails_needs_review", "emails", ["needs_review"])

    op.create_table(
        "interviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email_id", sa.Integer(), sa.ForeignKey("emails.id", ondelete="SET NULL")),
        sa.Column("scheduled_at", UTCDateTime()),
        sa.Column("meeting_link", sa.String(1000)),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_interviews_application_id", "interviews", ["application_id"])

    op.create_table(
        "status_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("old_status", sa.String(20)),
        sa.Column("new_status", sa.String(20), nullable=False),
        sa.Column("email_id", sa.Integer(), sa.ForeignKey("emails.id", ondelete="SET NULL")),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_status_events_application_id", "status_events", ["application_id"])

    op.create_table(
        "sync_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_history_id", sa.String(32)),
        sa.Column("last_run_at", UTCDateTime()),
    )

    op.create_table(
        "resumes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(300), nullable=False),
        sa.Column("parsed_text", sa.Text(), nullable=False),
        sa.Column("uploaded_at", UTCDateTime(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
    )

    op.create_table(
        "outreach_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("jd_text", sa.Text(), nullable=False),
        sa.Column("company", sa.String(200)),
        sa.Column("role", sa.String(300)),
        sa.Column("resume_id", sa.Integer(), sa.ForeignKey("resumes.id", ondelete="SET NULL")),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )

    op.create_table(
        "outreach_emails",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("outreach_campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("recipient_name", sa.String(200)),
        sa.Column("recipient_company", sa.String(200)),
        sa.Column("recipient_title", sa.String(200)),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attach_resume", sa.Boolean(), nullable=False),
        sa.Column("gmail_message_id", sa.String(64)),
        sa.Column("thread_id", sa.String(64)),
        sa.Column("sent_at", UTCDateTime()),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(OUTREACH_STATUSES, name="ck_outreach_emails_status"),
    )
    op.create_index("ix_outreach_emails_campaign_id", "outreach_emails", ["campaign_id"])
    op.create_index("ix_outreach_emails_recipient_email", "outreach_emails", ["recipient_email"])
    op.create_index("ix_outreach_emails_thread_id", "outreach_emails", ["thread_id"])


def downgrade() -> None:
    for table in (
        "outreach_emails",
        "outreach_campaigns",
        "resumes",
        "sync_state",
        "status_events",
        "interviews",
        "emails",
        "applications",
    ):
        op.drop_table(table)
