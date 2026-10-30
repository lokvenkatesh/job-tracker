"""Throttled cold-mail sender.

A scheduler tick calls send_next() every ~30 seconds. Each tick sends at most ONE email, and only:
- an `approved` email in a campaign where you clicked "Send approved" (send_requested_at set),
- once the random 60-180 s gap since the previous send has passed,
- while fewer than OUTREACH_DAILY_CAP emails went out today (your timezone),
- to someone not emailed within OUTREACH_DEDUPE_DAYS (unless you overrode that, or it's a
  follow-up in the same thread).

Each message has exactly one To: address and no Cc/Bcc. The attempt is recorded before calling
Gmail, so if the process dies mid-send the next tick finds the message in Sent (by recipient and
subject) instead of sending it twice. Gmail assigns its own Message-Id; it's read back after each
send for follow-up threading. Lookups use the read-only token; only this module uses gmail.send.
"""

import base64
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    MailAccount,
    OutreachCampaign,
    OutreachEmail,
    OutreachStatus,
    Resume,
    SyncStateRow,
    utcnow,
)
from app.gmail.accounts import AccountError
from app.outreach.recipients import recently_contacted

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
SENT_STATUSES = (OutreachStatus.SENT.value, OutreachStatus.REPLIED.value)


@dataclass(frozen=True)
class SendResult:
    action: str  # sent | recovered | waiting | cap_reached | idle | skipped | failed
    email_id: int | None = None
    detail: str = ""


def build_message(
    email: OutreachEmail,
    *,
    resume: Resume | None,
    reply_to: OutreachEmail | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = email.recipient_email  # one recipient, always; never Cc/Bcc
    msg["Subject"] = email.subject
    if reply_to is not None and reply_to.message_id_header:
        msg["In-Reply-To"] = reply_to.message_id_header
        msg["References"] = reply_to.message_id_header
    msg.set_content(email.body)
    if email.attach_resume and resume is not None and resume.content:
        maintype, _, subtype = (resume.content_type or "application/octet-stream").partition("/")
        msg.add_attachment(
            resume.content, maintype=maintype, subtype=subtype, filename=resume.filename
        )
    return msg


def sent_today(db: Session, now: datetime, timezone: str) -> int:
    tz = ZoneInfo(timezone)
    midnight = datetime.combine(now.astimezone(tz).date(), time(0), tzinfo=tz)
    return db.scalar(
        select(func.count()).where(
            OutreachEmail.status.in_(SENT_STATUSES), OutreachEmail.sent_at >= midnight
        )
    )


def _state(db: Session) -> SyncStateRow:
    row = db.get(SyncStateRow, 1)
    if row is None:
        row = SyncStateRow(id=1)
        db.add(row)
    return row


def _next_queued(db: Session) -> OutreachEmail | None:
    stmt = (
        select(OutreachEmail)
        .join(OutreachCampaign, OutreachEmail.campaign_id == OutreachCampaign.id)
        .where(
            OutreachEmail.status == OutreachStatus.APPROVED.value,
            OutreachCampaign.send_requested_at.is_not(None),
        )
        .order_by(OutreachCampaign.send_requested_at, OutreachEmail.id)
        .limit(1)
    )
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True, of=OutreachEmail)
    return db.scalars(stmt).first()


def _already_in_sent(read_service, email: OutreachEmail) -> dict | None:
    """Find a previous attempt in Sent (read-only) so an interrupted send isn't repeated.
    Gmail replaces our Message-ID on send, so match on recipient + exact subject instead; the
    30-day dedupe makes that pair unique in practice."""
    subject = email.subject.replace('"', "")
    q = f'in:sent to:{email.recipient_email} subject:"{subject}" newer_than:2d'
    resp = read_service.users().messages().list(userId="me", q=q, maxResults=1).execute()
    found = resp.get("messages") or []
    return found[0] if found else None


def _real_message_id(read_service, gmail_id: str) -> str | None:
    """The Message-Id Gmail assigned, needed for In-Reply-To on follow-ups."""
    try:
        m = (
            read_service.users()
            .messages()
            .get(userId="me", id=gmail_id, format="metadata", metadataHeaders=["Message-Id"])
            .execute()
        )
    except HttpError:
        return None
    headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
    return headers.get("message-id")


def sending_account(db: Session, email: OutreachEmail) -> MailAccount | None:
    """The inbox an email goes out from: a follow-up uses the original's inbox (same thread),
    otherwise the campaign's chosen inbox, otherwise the first inbox allowed to send."""
    if email.followup_of_id:
        original = db.get(OutreachEmail, email.followup_of_id)
        if original is not None and original.mail_account_id:
            return db.get(MailAccount, original.mail_account_id)
    if email.mail_account_id:
        return db.get(MailAccount, email.mail_account_id)
    campaign = db.get(OutreachCampaign, email.campaign_id)
    if campaign is not None and campaign.mail_account_id:
        return db.get(MailAccount, campaign.mail_account_id)
    return db.scalars(
        select(MailAccount)
        .where(MailAccount.send_token.is_not(None), MailAccount.enabled.is_(True))
        .order_by(MailAccount.id)
    ).first()


def send_next(
    db: Session,
    *,
    settings: Settings,
    services_for: Callable[[MailAccount | None], tuple[object, object]] | None = None,
    send_service=None,
    read_service=None,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> SendResult:
    """Send at most one queued email. The caller commits. `services_for(inbox)` returns that
    inbox's (gmail.send, gmail.readonly) services; tests may pass fixed services instead."""
    now = now or utcnow()
    rng = rng or random.Random()
    state = _state(db)
    if state.outreach_next_send_at and now < state.outreach_next_send_at:
        return SendResult("waiting", detail=state.outreach_next_send_at.isoformat())
    if sent_today(db, now, settings.timezone) >= settings.outreach_daily_cap:
        return SendResult("cap_reached")

    email = _next_queued(db)
    if email is None:
        return SendResult("idle")

    # Re-check dedupe at send time: someone may have been emailed since the draft was made.
    if email.followup_of_id is None and not email.allow_recent:
        recent = recently_contacted(
            db, [email.recipient_email], now=now, days=settings.outreach_dedupe_days
        )
        if recent:
            email.status = OutreachStatus.SKIPPED.value
            email.skip_reason = f"emailed {recent[email.recipient_email]:%b %d}; not sent again"
            return SendResult("skipped", email.id, email.skip_reason)

    original = db.get(OutreachEmail, email.followup_of_id) if email.followup_of_id else None
    account = sending_account(db, email)
    if services_for is not None:
        try:
            send_service, read_service = services_for(account)
        except AccountError as e:
            email.send_error = str(e)[:300]
            state.outreach_next_send_at = now + timedelta(seconds=settings.outreach_max_gap_seconds)
            return SendResult("blocked", email.id, email.send_error)
    if account is not None:
        email.mail_account_id = account.id
    if email.send_attempts > 0 and email.send_error is None:
        # A previous attempt didn't record its outcome (crash mid-send): check Sent first.
        found = _already_in_sent(read_service, email)
        if found:
            _mark_sent(email, found, now)
            email.message_id_header = _real_message_id(read_service, found["id"])
            _schedule_next(state, now, settings, rng)
            return SendResult("recovered", email.id)
    email.send_attempts += 1
    email.send_error = None
    db.flush()
    db.commit()  # record the attempt before Gmail sees it (crash-safe resend check)

    campaign = db.get(OutreachCampaign, email.campaign_id)
    resume = db.get(Resume, campaign.resume_id) if campaign.resume_id else None
    message = build_message(email, resume=resume, reply_to=original)
    body = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    if original is not None and original.thread_id:
        body["threadId"] = original.thread_id
    try:
        sent = send_service.users().messages().send(userId="me", body=body).execute()
    except HttpError as e:
        status = getattr(e.resp, "status", 0)
        email.send_error = f"Gmail error {status}"
        if 400 <= status < 500 and status != 429 or email.send_attempts >= MAX_ATTEMPTS:
            email.status = OutreachStatus.SKIPPED.value
            email.skip_reason = f"send failed (Gmail error {status})"
        state.outreach_next_send_at = now + timedelta(seconds=settings.outreach_max_gap_seconds)
        log.warning("Send failed for outreach email %s: Gmail error %s", email.id, status)
        return SendResult("failed", email.id, email.send_error)

    _mark_sent(email, sent, now)
    email.message_id_header = _real_message_id(read_service, sent["id"])
    _schedule_next(state, now, settings, rng)
    log.info("Sent outreach email %s", email.id)
    return SendResult("sent", email.id)


def _mark_sent(email: OutreachEmail, gmail_message: dict, now: datetime) -> None:
    email.status = OutreachStatus.SENT.value
    email.gmail_message_id = gmail_message.get("id")
    email.thread_id = gmail_message.get("threadId") or email.thread_id
    email.sent_at = now
    email.send_error = None


def _schedule_next(
    state: SyncStateRow, now: datetime, settings: Settings, rng: random.Random
) -> None:
    gap = rng.uniform(settings.outreach_min_gap_seconds, settings.outreach_max_gap_seconds)
    state.outreach_next_send_at = now + timedelta(seconds=gap)


def queue_status(
    db: Session, campaign_id: int, settings: Settings, now: datetime | None = None
) -> dict:
    now = now or utcnow()
    queued = db.scalar(
        select(func.count()).where(
            OutreachEmail.campaign_id == campaign_id,
            OutreachEmail.status == OutreachStatus.APPROVED.value,
        )
    )
    remaining = max(settings.outreach_daily_cap - sent_today(db, now, settings.timezone), 0)
    avg_gap = (settings.outreach_min_gap_seconds + settings.outreach_max_gap_seconds) / 2
    return {
        "queued": queued,
        "remaining_today": remaining,
        "estimated_minutes": round(min(queued, remaining) * avg_gap / 60),
    }
