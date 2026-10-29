"""Recipient list parsing (paste or CSV), validation, and dedupe against recent outreach."""

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parseaddr

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import OutreachEmail, OutreachStatus

MAX_RECIPIENTS = 200
FIELDS = ("email", "name", "company", "title")
_HEADER_ALIASES = {
    "email": "email",
    "e-mail": "email",
    "email address": "email",
    "mail": "email",
    "name": "name",
    "full name": "name",
    "first name": "name",
    "company": "company",
    "organization": "company",
    "org": "company",
    "title": "title",
    "job title": "title",
    "role": "title",
    "position": "title",
}


@dataclass(frozen=True)
class Recipient:
    email: str
    name: str | None = None
    company: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class Rejected:
    line: int
    text: str
    reason: str


def _normalize_email(raw: str) -> str:
    # Syntax only; no DNS lookups (deliverability checks would leak the list and slow things).
    return validate_email(raw.strip(), check_deliverability=False).normalized.lower()


def _clean(value: str | None) -> str | None:
    value = " ".join((value or "").split())
    return value or None


def _rows(text: str) -> Iterable[tuple[int, list[str]]]:
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = "\t" if "\t" in first else ","
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), start=1):
        if any(cell.strip() for cell in row):
            yield i, row


def parse_recipients(text: str) -> tuple[list[Recipient], list[Rejected]]:
    """Accepts one recipient per line as `email`, `Name <email>`, or CSV/TSV columns
    email,name,company,title (any order when there's a header row)."""
    rows = list(_rows(text))
    columns = list(FIELDS)
    if rows:
        header = [_HEADER_ALIASES.get(c.strip().lower()) for c in rows[0][1]]
        if "email" in header:
            columns = [h or "" for h in header]
            rows = rows[1:]

    recipients: list[Recipient] = []
    rejected: list[Rejected] = []
    seen: set[str] = set()
    for line, row in rows:
        raw = ",".join(row)
        fields = dict(zip(columns, row, strict=False))
        email_field = fields.get("email", "")
        name = fields.get("name")
        if "<" in email_field or not re.search(r"@", email_field):
            # `Jane Doe <jane@x.com>` or a single free-form cell
            parsed_name, parsed_addr = parseaddr(email_field if "@" in email_field else raw)
            email_field, name = parsed_addr, name or parsed_name
        try:
            email = _normalize_email(email_field)
        except EmailNotValidError as e:
            rejected.append(Rejected(line, raw.strip()[:200], f"invalid email: {e}"))
            continue
        if email in seen:
            rejected.append(Rejected(line, raw.strip()[:200], "duplicate in this list"))
            continue
        seen.add(email)
        recipients.append(
            Recipient(
                email=email,
                name=_clean(name),
                company=_clean(fields.get("company")),
                title=_clean(fields.get("title")),
            )
        )
        if len(recipients) > MAX_RECIPIENTS:
            raise ValueError(f"At most {MAX_RECIPIENTS} recipients per campaign.")
    return recipients, rejected


def recently_contacted(
    session: Session, emails: Iterable[str], *, now: datetime, days: int
) -> dict[str, datetime]:
    """Recipients emailed within `days` (sent or replied), with their last send time."""
    emails = list(emails)
    if not emails or days <= 0:
        return {}
    rows = session.execute(
        select(OutreachEmail.recipient_email, func.max(OutreachEmail.sent_at))
        .where(
            OutreachEmail.recipient_email.in_(emails),
            OutreachEmail.status.in_([OutreachStatus.SENT.value, OutreachStatus.REPLIED.value]),
            OutreachEmail.sent_at >= now - timedelta(days=days),
        )
        .group_by(OutreachEmail.recipient_email)
    ).all()
    return dict(rows)
