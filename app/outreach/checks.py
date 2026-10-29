"""Deterministic checks on a generated cold email. Claude is asked to follow the rules; these make
sure it did. Anything that fails is shown on the review screen."""

import re

from rapidfuzz import fuzz

from app.outreach.recipients import Recipient

MIN_WORDS, MAX_WORDS = 90, 150
MAX_SUBJECT_CHARS = 70

BANNED_PHRASES = (
    "i hope this email finds you well",
    "i hope this finds you well",
    "i hope this message finds you well",
    "i am writing to express my interest",
    "i'm writing to express my interest",
    "i'm excited to apply",
    "i am excited to apply",
)
BUZZWORDS = (
    "synergy",
    "passionate",
    "results-driven",
    "detail-oriented",
    "self-starter",
    "team player",
    "go-getter",
    "dynamic",
    "cutting-edge",
    "think outside the box",
    "fast-paced",
    "leverage",
    "best-in-class",
    "rockstar",
    "ninja",
    "hardworking",
    "motivated",
)
_NUMBER_RE = re.compile(r"\$?\d[\d,.]*\s?(?:%|x|k|m|b|ms)?", re.IGNORECASE)
_TIME_ASK_RE = re.compile(r"\b(\d{1,2})[\s-]?(?:min|minute)", re.IGNORECASE)


def _norm(text: str) -> str:
    return text.lower().replace("’", "'").replace("‘", "'")


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][\w'’.-]*", text))


def opening_line(body: str) -> str:
    """First sentence after the greeting line."""
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    if (
        lines
        and re.match(r"^(hi|hello|hey|dear)\b", lines[0], re.IGNORECASE)
        and len(lines[0]) < 60
    ):
        lines = lines[1:]
    text = " ".join(lines)
    return re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""


def _digits(token: str) -> str:
    return re.sub(r"[^\d.]", "", token).strip(".")


def ungrounded_numbers(body: str, sources: str) -> list[str]:
    """Numbers in the email that don't appear in the resume, notes, or JD (invented metrics).
    The '15-minute chat' style ask is exempt."""
    exempt = {m.group(1) for m in _TIME_ASK_RE.finditer(body)}
    source_digits = {_digits(t) for t in _NUMBER_RE.findall(sources)} | {
        _digits(t).replace(",", "") for t in _NUMBER_RE.findall(sources)
    }
    found = []
    for token in _NUMBER_RE.findall(body):
        d = _digits(token)
        if not d or d in exempt:
            continue
        if d not in source_digits and d.replace(",", "") not in source_digits:
            found.append(token.strip())
    return found


def check_draft(
    subject: str,
    body: str,
    *,
    recipient: Recipient,
    sources: str,
    other_openings: list[str] = (),
) -> list[str]:
    issues = []
    lower = _norm(body)

    n = word_count(body)
    if not MIN_WORDS <= n <= MAX_WORDS:
        issues.append(f"{n} words (aim for {MIN_WORDS}-{MAX_WORDS})")
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            issues.append(f'uses a banned phrase: "{phrase}"')
    buzz = [w for w in BUZZWORDS if re.search(rf"\b{re.escape(w)}\b", lower)]
    if len(buzz) >= 2:
        issues.append(f"buzzwords: {', '.join(buzz)}")
    if body.count("!") > 1:
        issues.append(f"{body.count('!')} exclamation marks (max 1)")
    if re.search(r"\[[^\]]*\]|\{[^}]*\}|<[A-Z][^>]*>", body + subject):
        issues.append("contains a placeholder like [Name]")

    if not subject.strip():
        issues.append("missing subject")
    elif len(subject) > MAX_SUBJECT_CHARS:
        issues.append(f"subject is {len(subject)} characters (max {MAX_SUBJECT_CHARS})")

    if recipient.name:
        first = recipient.name.split()[0]
        if first.lower() not in lower:
            issues.append(f"doesn't address {first} by name")
    if recipient.company and _norm(recipient.company) not in lower:
        issues.append(f"doesn't mention {recipient.company}")

    invented = ungrounded_numbers(body + " " + subject, sources)
    if invented:
        issues.append(f"numbers not found in your resume or notes: {', '.join(invented)}")

    opening = opening_line(body)
    for other in other_openings:
        if opening and fuzz.ratio(_norm(opening), _norm(other)) >= 80:
            issues.append("opening line is nearly the same as another draft's")
            break
    return issues
