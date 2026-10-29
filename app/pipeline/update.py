"""update_db: write one classified email to the database and apply the state machine."""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.classifier.schema import Classification, EmailType
from app.db.matcher import MatchResult
from app.db.models import Application, Email, Interview, Status, StatusEvent
from app.db.state_machine import TYPE_TO_STATUS, transition
from app.gmail.message import EmailMessage
from app.notify.base import InterviewAlert


@dataclass(frozen=True)
class ApplyOutcome:
    email_id: int
    application_id: int | None
    action: str  # duplicate | stored:<type> | created | advanced | rejected | review:<reason> | ...
    alert: InterviewAlert | None = None


def apply_email(
    session: Session,
    msg: EmailMessage,
    c: Classification,
    match: MatchResult,
    *,
    threshold: float,
    timezone: str,
    embedding: list[float] | None = None,
) -> ApplyOutcome:
    existing = session.scalar(select(Email.id).where(Email.gmail_id == msg.gmail_id))
    if existing is not None:
        return ApplyOutcome(existing, None, "duplicate")

    received = msg.received_at
    email = Email(
        gmail_id=msg.gmail_id,
        thread_id=msg.thread_id,
        type=c.type.value,
        received_at=received,
        confidence=c.confidence,
        sender_email=msg.sender_email,
        subject=msg.subject[:1000],
        company=c.company,
        role=c.role,
        needs_review=False,
        embedding=embedding,
    )
    session.add(email)
    session.flush()

    matched = session.get(Application, match.application_id) if match.application_id else None

    def review(reason: str, app: Application | None = None) -> ApplyOutcome:
        email.needs_review = True
        email.review_reason = reason
        email.application_id = app.id if app else None
        return ApplyOutcome(email.id, email.application_id, f"review:{reason}")

    if c.needs_review(threshold):
        return review(f"error:{c.error}" if c.error else "low_confidence", matched)

    target = TYPE_TO_STATUS.get(c.type)
    if target is None:  # other / recruiter_outreach: stored, never an application
        if match.method == "thread" and matched is not None:
            email.application_id = matched.id
        return ApplyOutcome(email.id, email.application_id, f"stored:{c.type.value}")

    is_confirmation = c.type is EmailType.APPLICATION_CONFIRMATION
    if match.ambiguous and not (is_confirmation and match.reason == "role_mismatch"):
        return review(f"ambiguous_match:{match.reason}")

    app = matched
    if (
        app is not None
        and is_confirmation
        and app.status == Status.REJECTED
        and received > app.status_changed_at
    ):
        app = None  # re-applied after a rejection: a new application

    local_date = received.astimezone(ZoneInfo(timezone)).date()
    if app is None:
        if not c.company:
            return review("no_company")
        app = Application(
            company=c.company,
            role=c.role,
            status=target.value,
            applied_date=local_date if is_confirmation else None,
            last_update=received,
            status_changed_at=received,
            source=f"email:{c.type.value}",
            needs_review=False,
        )
        session.add(app)
        session.flush()
        session.add(StatusEvent(application_id=app.id, new_status=target.value, email_id=email.id))
        action = "created"
    else:
        t = transition(
            Status(app.status),
            target,
            email_at=received,
            status_changed_at=app.status_changed_at,
        )
        if t.new_status is not None:
            session.add(
                StatusEvent(
                    application_id=app.id,
                    old_status=app.status,
                    new_status=t.new_status.value,
                    email_id=email.id,
                )
            )
            app.status = t.new_status.value
            app.status_changed_at = received
        if t.needs_review:
            app.needs_review = True
            email.needs_review = True
            email.review_reason = t.reason
        if app.role is None and c.role:
            app.role = c.role
        if is_confirmation and app.applied_date is None:
            app.applied_date = local_date
        action = t.reason

    email.application_id = app.id
    app.last_update = max(app.last_update, received)

    alert = None
    if c.type is EmailType.INTERVIEW_INVITE and not email.needs_review:
        alert = upsert_interview(session, app, email, c)
    return ApplyOutcome(email.id, app.id, action, alert)


def upsert_interview(
    session: Session, app: Application, email: Email, c: Classification
) -> InterviewAlert | None:
    """A reschedule updates the pending interview instead of adding one. 'Pending' means no time
    yet, or a time still after this email arrived; once an interview has happened, the next
    invite is a new round."""
    received: datetime = email.received_at
    pending = session.scalar(
        select(Interview)
        .where(
            Interview.application_id == app.id,
            or_(Interview.scheduled_at.is_(None), Interview.scheduled_at > received),
        )
        .order_by(Interview.created_at.desc())
        .limit(1)
    )

    def alert(rescheduled: bool, interview: Interview) -> InterviewAlert:
        return InterviewAlert(
            application_id=app.id,
            company=app.company,
            role=app.role,
            scheduled_at=interview.scheduled_at,
            meeting_link=interview.meeting_link,
            rescheduled=rescheduled,
        )

    if pending is None:
        interview = Interview(
            application_id=app.id,
            email_id=email.id,
            scheduled_at=c.interview_datetime,
            meeting_link=c.meeting_link,
            created_at=received,
            updated_at=received,
        )
        session.add(interview)
        return alert(False, interview)

    if received < pending.updated_at:
        return None  # older than what we already know
    new_time = c.interview_datetime is not None and c.interview_datetime != pending.scheduled_at
    new_link = c.meeting_link is not None and c.meeting_link != pending.meeting_link
    rescheduled = new_time and pending.scheduled_at is not None
    if new_time:
        pending.scheduled_at = c.interview_datetime
    if new_link:
        pending.meeting_link = c.meeting_link
    pending.email_id = email.id
    pending.updated_at = received
    return alert(rescheduled, pending) if new_time or new_link else None
