"""Follow-up drafts for cold emails with no reply after FOLLOWUP_BUSINESS_DAYS business days.

Follow-ups are created as drafts in the same campaign and thread; like every draft they need your
approval and a "Send approved" click before anything goes out. One follow-up per email, ever.
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.db.models import OutreachCampaign, OutreachEmail, OutreachStatus, Resume, utcnow
from app.db.session import session_scope
from app.outreach.generator import DraftGenerator, GenerationError, followup_subject, sources_text
from app.outreach.recipients import Recipient

log = logging.getLogger(__name__)

MAX_PER_RUN = 10


def business_days_between(start: date, end: date) -> int:
    """Weekdays after `start` up to and including `end`."""
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def due_for_followup(
    db: Session, *, now: datetime, timezone: str, business_days: int
) -> list[OutreachEmail]:
    tz = ZoneInfo(timezone)
    followup = aliased(OutreachEmail)
    candidates = db.scalars(
        select(OutreachEmail)
        .outerjoin(followup, followup.followup_of_id == OutreachEmail.id)
        .where(
            OutreachEmail.status == OutreachStatus.SENT.value,
            OutreachEmail.followup_of_id.is_(None),  # never follow up a follow-up
            OutreachEmail.sent_at.is_not(None),
            OutreachEmail.send_error.is_(None),  # bounced mail gets no follow-up
            followup.id.is_(None),
        )
        .order_by(OutreachEmail.sent_at)
    ).all()
    today = now.astimezone(tz).date()
    return [
        e
        for e in candidates
        if business_days_between(e.sent_at.astimezone(tz).date(), today) >= business_days
    ]


def write_followup(
    db: Session, followup: OutreachEmail, generator: DraftGenerator, instructions=None
) -> None:
    original = db.get(OutreachEmail, followup.followup_of_id)
    campaign = db.get(OutreachCampaign, followup.campaign_id)
    resume = db.get(Resume, campaign.resume_id) if campaign.resume_id else None
    try:
        draft = generator.followup(
            campaign.brief or {},
            Recipient(
                followup.recipient_email,
                followup.recipient_name,
                followup.recipient_company,
                followup.recipient_title,
            ),
            original_subject=original.subject,
            original_body=original.body,
            sources=sources_text(
                campaign.jd_text, resume.parsed_text if resume else "", campaign.notes
            ),
            instructions=instructions,
        )
        followup.subject, followup.body, followup.issues = draft.subject, draft.body, draft.issues
    except (GenerationError, anthropic.APIError) as e:
        followup.issues = [
            f"Generation failed: {e if isinstance(e, GenerationError) else 'API error'}"
        ]
    followup.status = OutreachStatus.DRAFT.value
    followup.generated_at = utcnow()


def create_followups(
    session_factory: sessionmaker[Session],
    generator: DraftGenerator,
    *,
    timezone: str,
    business_days: int,
    now: datetime | None = None,
) -> int:
    now = now or utcnow()
    with session_scope(session_factory) as db:
        due = [
            e.id
            for e in due_for_followup(db, now=now, timezone=timezone, business_days=business_days)
        ]
    created = 0
    for original_id in due[:MAX_PER_RUN]:
        with session_scope(session_factory) as db:
            original = db.get(OutreachEmail, original_id)
            if original is None or original.status != OutreachStatus.SENT.value:
                continue  # replied in the meantime
            followup = OutreachEmail(
                campaign_id=original.campaign_id,
                recipient_email=original.recipient_email,
                recipient_name=original.recipient_name,
                recipient_company=original.recipient_company,
                recipient_title=original.recipient_title,
                subject=followup_subject(original.subject),
                body="",
                status=OutreachStatus.DRAFT.value,
                attach_resume=False,
                allow_recent=True,
                followup_of_id=original.id,
            )
            db.add(followup)
            db.flush()
            write_followup(db, followup, generator)
            created += 1
    if created:
        log.info("Drafted %d follow-ups", created)
    return created
