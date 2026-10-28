"""Cheap rule-based gate before any LLM call. Errs toward recall: the classifier labels noise as
'other', but anything dropped here is never seen again."""

import re
from collections.abc import Container
from dataclasses import dataclass

from app.gmail.message import EmailMessage

ATS_DOMAINS = (
    "greenhouse.io",
    "greenhouse-mail.io",
    "lever.co",
    "myworkdayjobs.com",
    "myworkday.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "icims.com",
    "linkedin.com",
)

KEYWORD_RE = re.compile(
    r"\b(applications?|applied|applying|interview(?:s|ing|ed)?|unfortunately|next steps?"
    r"|assessments?|offers?)\b",
    re.IGNORECASE,
)

# Our own outgoing mail and junk never describe an application status.
DROP_LABELS = frozenset({"SENT", "DRAFT", "SPAM", "TRASH", "CHAT"})


@dataclass(frozen=True)
class PrefilterDecision:
    passed: bool
    reason: str


def match_ats_domain(domain: str) -> str | None:
    domain = domain.lower()
    for ats in ATS_DOMAINS:
        if domain == ats or domain.endswith("." + ats):
            return ats
    return None


def prefilter(
    msg: EmailMessage, *, tracked_thread_ids: Container[str] = frozenset()
) -> PrefilterDecision:
    """tracked_thread_ids: threads we already follow (applications, sent outreach) — replies on
    them always pass, even without keywords."""
    dropped = DROP_LABELS.intersection(msg.label_ids)
    if dropped:
        return PrefilterDecision(False, f"label:{min(dropped)}")
    if msg.thread_id in tracked_thread_ids:
        return PrefilterDecision(True, "tracked_thread")
    if ats := match_ats_domain(msg.sender_domain):
        return PrefilterDecision(True, f"ats:{ats}")
    if m := KEYWORD_RE.search(f"{msg.subject}\n{msg.snippet}"):
        return PrefilterDecision(True, f"keyword:{m.group(1).lower()}")
    return PrefilterDecision(False, "no_match")
