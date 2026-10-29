"""Request/response models for the JSON API."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.db.models import Status


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ApplicationOut(_ORM):
    id: int
    company: str
    role: str | None
    status: Status
    applied_date: date | None
    last_update: datetime
    source: str
    needs_review: bool
    created_at: datetime


class EmailOut(_ORM):
    id: int
    gmail_id: str
    thread_id: str
    application_id: int | None
    type: str
    received_at: datetime
    confidence: float
    sender_email: str
    subject: str
    company: str | None
    role: str | None
    needs_review: bool
    review_reason: str | None

    @computed_field
    @property
    def gmail_url(self) -> str:
        return f"https://mail.google.com/mail/u/0/#all/{self.gmail_id}"


class StatusEventOut(_ORM):
    id: int
    old_status: Status | None
    new_status: Status
    email_id: int | None
    created_at: datetime


class InterviewOut(_ORM):
    id: int
    application_id: int
    scheduled_at: datetime | None
    meeting_link: str | None
    updated_at: datetime


class ApplicationDetail(ApplicationOut):
    events: list[StatusEventOut]
    emails: list[EmailOut]
    interviews: list[InterviewOut]


class ApplicationList(BaseModel):
    total: int
    items: list[ApplicationOut]


class ApplicationPatch(BaseModel):
    """Manual edits. A manual status change is trusted as-is (it may move backward)."""

    model_config = ConfigDict(extra="forbid")

    company: str | None = Field(None, min_length=1, max_length=200)
    role: str | None = Field(None, max_length=300)
    status: Status | None = None
    applied_date: date | None = None
    needs_review: bool | None = None


class ReviewQueue(BaseModel):
    emails: list[EmailOut]
    applications: list[ApplicationOut]


class ReviewAction(BaseModel):
    """Resolve a review-queue email.
    - dismiss: not an application update; clear the flag
    - link: attach to an existing application and apply its status change
    - create: start a new application from it
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["dismiss", "link", "create"]
    application_id: int | None = None
    company: str | None = Field(None, min_length=1, max_length=200)
    role: str | None = Field(None, max_length=300)
    status: Status | None = None


class FunnelStage(BaseModel):
    stage: Status
    reached: int


class WeekCount(BaseModel):
    week_start: date
    applications: int


class UpcomingInterview(BaseModel):
    application_id: int
    company: str
    role: str | None
    scheduled_at: datetime | None
    meeting_link: str | None


class Stats(BaseModel):
    total_applications: int
    by_status: dict[Status, int]
    funnel: list[FunnelStage]
    response_rate: float  # share of applications that got any reply beyond the confirmation
    interview_rate: float  # share that reached the interview stage
    needs_review: int
    recruiter_outreach: int
    weekly: list[WeekCount]
    upcoming_interviews: list[UpcomingInterview]
