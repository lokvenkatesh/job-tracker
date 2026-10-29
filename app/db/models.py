"""SQLAlchemy models. Postgres + pgvector in production; the Embedding type falls back to JSON on
other dialects so tests can run on SQLite."""

from datetime import UTC, date, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5


def utcnow() -> datetime:
    return datetime.now(UTC)


class Status(StrEnum):
    APPLIED = "applied"
    ASSESSMENT = "assessment"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"


class OutreachStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"
    SKIPPED = "skipped"
    REPLIED = "replied"


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(e.value) for e in enum)})"


class Embedding(TypeDecorator):
    impl = JSON
    cache_ok = True
    comparator_factory = Vector.comparator_factory  # cosine_distance() etc. (Postgres only)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(EMBEDDING_DIM))
        return dialect.type_descriptor(JSON())

    def process_result_value(self, value, dialect):
        return None if value is None else [float(x) for x in value]


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetimes on every dialect (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetime")
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (CheckConstraint(_in("status", Status), name="ck_applications_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20))
    applied_date: Mapped[date | None] = mapped_column(Date)
    last_update: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # When status last changed; emails older than this never change status.
    status_changed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    source: Mapped[str] = mapped_column(String(50))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    emails: Mapped[list["Email"]] = relationship(back_populates="application")
    interviews: Mapped[list["Interview"]] = relationship(back_populates="application")


class Email(Base):
    """One row per processed Gmail message (every classified one, including 'other'), which is
    what makes re-processing idempotent. Bodies are not stored."""

    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    gmail_id: Mapped[str] = mapped_column(String(64), unique=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), index=True
    )
    type: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())
    confidence: Mapped[float] = mapped_column(Float)
    sender_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(1000))
    company: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(300))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_reason: Mapped[str | None] = mapped_column(String(100))
    embedding: Mapped[list[float] | None] = mapped_column(Embedding())
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    application: Mapped[Application | None] = relationship(back_populates="emails")


class Interview(Base):
    __tablename__ = "interviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id", ondelete="SET NULL"))
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    meeting_link: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    application: Mapped[Application] = relationship(back_populates="interviews")


class StatusEvent(Base):
    __tablename__ = "status_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    old_status: Mapped[str | None] = mapped_column(String(20))
    new_status: Mapped[str] = mapped_column(String(20))
    email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class SyncStateRow(Base):
    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_history_id: Mapped[str | None] = mapped_column(String(32))
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    parsed_text: Mapped[str] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class OutreachCampaign(Base):
    __tablename__ = "outreach_campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    jd_text: Mapped[str] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(300))
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class OutreachEmail(Base):
    __tablename__ = "outreach_emails"
    __table_args__ = (
        CheckConstraint(_in("status", OutreachStatus), name="ck_outreach_emails_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_campaigns.id", ondelete="CASCADE"), index=True
    )
    recipient_email: Mapped[str] = mapped_column(String(320), index=True)
    recipient_name: Mapped[str | None] = mapped_column(String(200))
    recipient_company: Mapped[str | None] = mapped_column(String(200))
    recipient_title: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=OutreachStatus.DRAFT.value)
    attach_resume: Mapped[bool] = mapped_column(Boolean, default=False)
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))
    thread_id: Mapped[str | None] = mapped_column(String(64), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
