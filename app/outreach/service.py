"""Cold-mail campaigns: create, generate drafts, edit, regenerate, approve. Sending is step 8.

Nothing here sends mail. A draft only becomes `approved` through an explicit user action, and any
edit to an approved draft's text puts it back to `draft` so approval always covers what goes out.
"""

import logging
from datetime import datetime

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import OutreachCampaign, OutreachEmail, OutreachStatus, Resume, utcnow
from app.db.session import session_scope
from app.outreach.checks import check_draft, opening_line
from app.outreach.generator import DraftGenerator, GenerationError, sources_text
from app.outreach.recipients import Recipient, Rejected, parse_recipients, recently_contacted
from app.outreach.resume import parse_resume

log = logging.getLogger(__name__)

EDITABLE = {OutreachStatus.DRAFT.value, OutreachStatus.APPROVED.value, OutreachStatus.SKIPPED.value}
FAILED_PREFIX = "Generation failed"


class OutreachError(ValueError):
    pass


def save_resume(db: Session, filename: str, data: bytes, *, make_default: bool = True) -> Resume:
    text, content_type = parse_resume(filename, data)
    if make_default:
        for r in db.scalars(select(Resume).where(Resume.is_default.is_(True))):
            r.is_default = False
    resume = Resume(
        filename=filename[:300],
        parsed_text=text,
        content=data,
        content_type=content_type,
        is_default=make_default,
    )
    db.add(resume)
    db.flush()
    return resume


def default_resume(db: Session) -> Resume | None:
    return db.scalars(
        select(Resume).order_by(Resume.is_default.desc(), Resume.uploaded_at.desc()).limit(1)
    ).first()


def create_campaign(
    db: Session,
    *,
    jd_text: str,
    recipients_text: str,
    resume_id: int | None,
    notes: str | None,
    attach_resume: bool,
    override_recent: bool,
    dedupe_days: int,
    now: datetime | None = None,
) -> tuple[OutreachCampaign, list[Rejected]]:
    now = now or utcnow()
    if len(jd_text.strip()) < 50:
        raise OutreachError("Paste the full job description.")
    recipients, rejected = parse_recipients(recipients_text)
    if not recipients:
        raise OutreachError("No valid recipient emails found.")
    resume = db.get(Resume, resume_id) if resume_id else default_resume(db)
    if resume is None:
        raise OutreachError("Upload a resume first.")

    campaign = OutreachCampaign(
        jd_text=jd_text.strip(), resume_id=resume.id, notes=(notes or "").strip() or None
    )
    db.add(campaign)
    db.flush()
    recent = (
        {}
        if override_recent
        else recently_contacted(db, [r.email for r in recipients], now=now, days=dedupe_days)
    )
    for r in recipients:
        last = recent.get(r.email)
        db.add(
            OutreachEmail(
                campaign_id=campaign.id,
                recipient_email=r.email,
                recipient_name=r.name,
                recipient_company=r.company,
                recipient_title=r.title,
                subject="",
                body="",
                attach_resume=attach_resume,
                status=OutreachStatus.SKIPPED.value if last else OutreachStatus.DRAFT.value,
                skip_reason=f"emailed {last:%b %d} (within {dedupe_days} days)" if last else None,
            )
        )
    db.flush()
    return campaign, rejected


def _recipient(e: OutreachEmail) -> Recipient:
    return Recipient(e.recipient_email, e.recipient_name, e.recipient_company, e.recipient_title)


def _openings(db: Session, campaign_id: int, exclude_id: int | None = None) -> list[str]:
    bodies = db.scalars(
        select(OutreachEmail.body).where(
            OutreachEmail.campaign_id == campaign_id,
            OutreachEmail.body != "",
            OutreachEmail.id != (exclude_id or -1),
        )
    )
    return [o for o in (opening_line(b) for b in bodies) if o]


def _ensure_brief(db: Session, campaign: OutreachCampaign, generator: DraftGenerator) -> dict:
    if campaign.brief is None:
        resume = db.get(Resume, campaign.resume_id)
        if resume is None:
            raise GenerationError("the resume for this campaign was deleted")
        brief = generator.brief(campaign.jd_text, resume.parsed_text, campaign.notes)
        campaign.brief = brief
        campaign.company = brief.get("company") or None
        campaign.role = brief.get("role") or None
    return campaign.brief


def _write_draft(
    db: Session,
    email: OutreachEmail,
    campaign: OutreachCampaign,
    generator: DraftGenerator,
    instructions: str | None = None,
) -> None:
    resume = db.get(Resume, campaign.resume_id)
    try:
        brief = _ensure_brief(db, campaign, generator)
        draft = generator.draft(
            brief,
            _recipient(email),
            notes=campaign.notes,
            sources=sources_text(
                campaign.jd_text, resume.parsed_text if resume else "", campaign.notes
            ),
            other_openings=_openings(db, campaign.id, exclude_id=email.id),
            instructions=instructions,
        )
        email.subject, email.body, email.issues = draft.subject[:300], draft.body, draft.issues
    except (GenerationError, anthropic.APIError) as e:
        log.warning("Draft generation failed for outreach email %s: %s", email.id, type(e).__name__)
        email.issues = [f"{FAILED_PREFIX}: {e if isinstance(e, GenerationError) else 'API error'}"]
    email.status = OutreachStatus.DRAFT.value
    email.generated_at = utcnow()


def generate_pending(
    session_factory: sessionmaker[Session], campaign_id: int, generator: DraftGenerator
) -> None:
    """Background job: write every pending draft, one transaction each, so the review screen
    fills in as drafts finish. Drafts are written in order so each sees earlier openings."""
    with session_scope(session_factory) as db:
        ids = list(
            db.scalars(
                select(OutreachEmail.id)
                .where(
                    OutreachEmail.campaign_id == campaign_id,
                    OutreachEmail.status == OutreachStatus.DRAFT.value,
                    OutreachEmail.generated_at.is_(None),
                )
                .order_by(OutreachEmail.id)
            )
        )
    for email_id in ids:
        with session_scope(session_factory) as db:
            email = db.get(OutreachEmail, email_id)
            if email is None or email.generated_at is not None:
                continue
            _write_draft(db, email, db.get(OutreachCampaign, campaign_id), generator)


def regenerate(
    db: Session, email: OutreachEmail, generator: DraftGenerator, instructions: str | None
) -> OutreachEmail:
    if email.status not in EDITABLE:
        raise OutreachError("This email was already sent.")
    _write_draft(db, email, db.get(OutreachCampaign, email.campaign_id), generator, instructions)
    db.flush()
    return email


def recheck(db: Session, email: OutreachEmail) -> list[str]:
    campaign = db.get(OutreachCampaign, email.campaign_id)
    resume = db.get(Resume, campaign.resume_id) if campaign.resume_id else None
    return check_draft(
        email.subject,
        email.body,
        recipient=_recipient(email),
        sources=sources_text(
            campaign.jd_text, resume.parsed_text if resume else "", campaign.notes
        ),
        other_openings=_openings(db, campaign.id, exclude_id=email.id),
    )


def update_email(
    db: Session,
    email: OutreachEmail,
    *,
    subject: str | None = None,
    body: str | None = None,
    status: OutreachStatus | None = None,
    attach_resume: bool | None = None,
) -> OutreachEmail:
    if email.status not in EDITABLE:
        raise OutreachError("This email was already sent and can't be changed.")
    text_changed = (subject is not None and subject.strip() != email.subject) or (
        body is not None and body.strip() != email.body
    )
    if subject is not None:
        email.subject = subject.strip()[:300]
    if body is not None:
        email.body = body.strip()
    if attach_resume is not None:
        email.attach_resume = attach_resume
    if text_changed:
        email.issues = recheck(db, email)
        if email.status == OutreachStatus.APPROVED.value and status is None:
            email.status = OutreachStatus.DRAFT.value  # approval must cover the final text
    if status is not None:
        if status not in (OutreachStatus.DRAFT, OutreachStatus.APPROVED, OutreachStatus.SKIPPED):
            raise OutreachError("Only draft, approved, or skipped can be set here.")
        if status is OutreachStatus.APPROVED:
            if email.generated_at is None:
                raise OutreachError("This draft is still being written.")
            if not email.subject or not email.body:
                raise OutreachError("A draft needs a subject and a body before it can be approved.")
        email.status = status.value
        if status is not OutreachStatus.SKIPPED:
            email.skip_reason = None
    db.flush()
    return email


def approve_all(db: Session, campaign_id: int) -> tuple[int, int]:
    """Approve every finished draft that passed all checks. Drafts with open issues are held
    back for a look (they can still be approved one by one). Returns (approved, held_back)."""
    approved = held = 0
    for email in db.scalars(
        select(OutreachEmail).where(
            OutreachEmail.campaign_id == campaign_id,
            OutreachEmail.status == OutreachStatus.DRAFT.value,
        )
    ):
        if email.generated_at is None or not email.subject or not email.body or email.issues:
            held += 1
            continue
        email.status = OutreachStatus.APPROVED.value
        approved += 1
    db.flush()
    return approved, held
