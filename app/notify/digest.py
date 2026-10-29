"""Daily digest: what changed in the last 24 hours, plus upcoming interviews.

Changes are timed by the email that caused them (falling back to when the change was recorded for
manual edits), so a backfill of old mail doesn't flood today's digest.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Application, Email, Interview, Status, StatusEvent
from app.notify.slack import escape, format_when, link

MAX_LISTED = 15
ADVANCED = {Status.ASSESSMENT, Status.INTERVIEW, Status.OFFER}


@dataclass(frozen=True)
class DigestEntry:
    company: str
    role: str | None
    status: Status


@dataclass(frozen=True)
class DigestInterview:
    company: str
    role: str | None
    scheduled_at: datetime | None
    meeting_link: str | None


@dataclass(frozen=True)
class Digest:
    now: datetime
    new_applications: list[DigestEntry]
    rejections: list[DigestEntry]
    advanced: list[DigestEntry]
    upcoming: list[DigestInterview]
    needs_review: int
    total_applications: int
    response_rate: float

    @property
    def quiet(self) -> bool:
        return not (self.new_applications or self.rejections or self.advanced or self.upcoming)


def build_digest(
    session: Session,
    *,
    now: datetime,
    window: timedelta = timedelta(hours=24),
    upcoming_days: int = 7,
) -> Digest:
    since = now - window
    when = func.coalesce(Email.received_at, StatusEvent.created_at)
    rows = session.execute(
        select(StatusEvent, Application)
        .join(Application, StatusEvent.application_id == Application.id)
        .outerjoin(Email, StatusEvent.email_id == Email.id)
        .where(when > since, when <= now)
        .order_by(when, StatusEvent.id)
    ).all()

    new_apps, rejections, advanced = [], [], []
    for event, app in rows:
        entry = DigestEntry(app.company, app.role, Status(event.new_status))
        if entry.status is Status.REJECTED:
            rejections.append(entry)
        elif entry.status in ADVANCED:
            advanced.append(entry)
        elif event.old_status is None:
            new_apps.append(entry)

    upcoming = session.execute(
        select(Interview, Application)
        .join(Application, Interview.application_id == Application.id)
        .where(Application.status != Status.REJECTED.value)
        .where(
            or_(
                Interview.scheduled_at.is_(None),
                Interview.scheduled_at.between(now, now + timedelta(days=upcoming_days)),
            )
        )
        .order_by(Interview.scheduled_at.asc().nulls_last())
    ).all()

    total = session.scalar(select(func.count()).select_from(Application)) or 0
    responded = session.scalar(
        select(func.count(func.distinct(StatusEvent.application_id))).where(
            StatusEvent.new_status != Status.APPLIED.value
        )
    )
    review = session.scalar(select(func.count()).where(Email.needs_review.is_(True))) or 0
    review += session.scalar(select(func.count()).where(Application.needs_review.is_(True))) or 0

    return Digest(
        now=now,
        new_applications=new_apps,
        rejections=rejections,
        advanced=advanced,
        upcoming=[
            DigestInterview(a.company, a.role, i.scheduled_at, i.meeting_link) for i, a in upcoming
        ],
        needs_review=review,
        total_applications=total,
        response_rate=(responded or 0) / total if total else 0.0,
    )


def _entry(e: DigestEntry, *, show_status: bool = False) -> str:
    text = f"• *{escape(e.company)}*"
    if e.role:
        text += f" — {escape(e.role)}"
    if show_status:
        text += f" → {e.status.value}"
    return text


def _section(title: str, lines: list[str]) -> list[dict]:
    shown = lines[:MAX_LISTED]
    if len(lines) > MAX_LISTED:
        shown.append(f"_…and {len(lines) - MAX_LISTED} more_")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join(shown)}}
    ]


def digest_message(d: Digest, timezone: str) -> dict:
    local = d.now.astimezone(ZoneInfo(timezone))
    title = f"Job search digest — {local:%a %b} {local.day}"
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": title}}]

    if d.advanced:
        blocks += _section(
            f"Moved forward ({len(d.advanced)})", [_entry(e, show_status=True) for e in d.advanced]
        )
    if d.upcoming:
        lines = []
        for i in d.upcoming:
            line = f"• {format_when(i.scheduled_at, timezone)} — *{escape(i.company)}*"
            if i.role:
                line += f" ({escape(i.role)})"
            if i.meeting_link:
                line += f" · {link(i.meeting_link, 'Join')}"
            lines.append(line)
        blocks += _section(f"Upcoming interviews ({len(d.upcoming)})", lines)
    if d.new_applications:
        blocks += _section(
            f"New applications ({len(d.new_applications)})", [_entry(e) for e in d.new_applications]
        )
    if d.rejections:
        blocks += _section(f"Rejections ({len(d.rejections)})", [_entry(e) for e in d.rejections])
    if d.quiet:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "No changes in the last 24 hours."},
            }
        )

    footer = f"{d.total_applications} applications tracked · {d.response_rate:.0%} got a response"
    if d.needs_review:
        footer += f" · *{d.needs_review} waiting for review*"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})

    summary = (
        f"{len(d.new_applications)} new, {len(d.rejections)} rejections, "
        f"{len(d.advanced)} moved forward, {len(d.upcoming)} upcoming interviews"
    )
    return {"text": f"{title}: {summary}", "blocks": blocks}


def digest_text(d: Digest, timezone: str) -> str:
    """Plain-text rendering of the Slack message, for previews in the terminal."""
    message = digest_message(d, timezone)
    parts = []
    for block in message["blocks"]:
        if block["type"] == "header":
            parts.append(block["text"]["text"])
        elif block["type"] == "section":
            parts.append(block["text"]["text"])
        elif block["type"] == "context":
            parts.append(block["elements"][0]["text"])
    return "\n\n".join(parts).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
