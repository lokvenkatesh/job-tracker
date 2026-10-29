"""Reply tracking for sent cold emails, run by the hourly tracker for every new message."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import OutreachEmail, OutreachStatus
from app.gmail.message import EmailMessage

BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")


@dataclass(frozen=True)
class ReplyAlert:
    outreach_email_id: int
    recipient: str
    subject: str
    bounced: bool = False


def _domain(address: str) -> str:
    return address.rpartition("@")[2].lower()


def record_reply(
    session: Session, msg: EmailMessage, *, application_id: int | None = None
) -> ReplyAlert | None:
    """If msg answers one of our sent cold emails (same Gmail thread, from the recipient's
    address or domain), mark it replied. Bounces are recorded as a send error instead."""
    sent = session.scalars(
        select(OutreachEmail)
        .where(
            OutreachEmail.thread_id == msg.thread_id,
            OutreachEmail.status.in_([OutreachStatus.SENT.value, OutreachStatus.REPLIED.value]),
        )
        .order_by(OutreachEmail.sent_at.desc())
    ).all()
    if not sent:
        return None
    target = sent[0]
    if msg.sender_email.startswith(BOUNCE_SENDERS):
        target.send_error = "bounced"
        return ReplyAlert(target.id, target.recipient_email, target.subject, bounced=True)
    if target.sent_at and msg.received_at < target.sent_at:
        return None
    same_person = msg.sender_email == target.recipient_email
    if not same_person and _domain(msg.sender_email) != _domain(target.recipient_email):
        return None  # someone else on the thread (or your own message)
    first_reply = target.status != OutreachStatus.REPLIED.value
    target.status = OutreachStatus.REPLIED.value
    target.replied_at = target.replied_at or msg.received_at
    if application_id is not None and target.application_id is None:
        target.application_id = application_id
    # The whole conversation counts as replied: earlier mails and follow-ups in the thread.
    for other in sent[1:]:
        other.status = OutreachStatus.REPLIED.value
        other.replied_at = other.replied_at or msg.received_at
    if not first_reply:
        return None
    return ReplyAlert(target.id, target.recipient_email, target.subject)
