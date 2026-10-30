"""Cheap rule-based gate before any LLM call. Errs toward recall: the classifier labels noise as
'other', but anything dropped here is never seen again."""

import re
from collections.abc import Container, Iterable
from dataclasses import dataclass

from app.gmail.message import EmailMessage

ATS_DOMAINS = (
    # From the spec
    "greenhouse.io",
    "greenhouse-mail.io",
    "lever.co",
    "myworkdayjobs.com",
    "myworkday.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "icims.com",
    # Other common ATS senders
    "applytojob.com",  # JazzHR
    "workablemail.com",
    "teamtailor-mail.com",
    "ats.rippling.com",
    "successfactors.com",
    "oraclecloud.com",  # Oracle Recruiting Cloud
    "cloud.oracle.com",
    "applicant-tracking.com",
    "jobvite.com",
    "taleo.net",
    "bamboohr.com",
    "breezy.hr",
    "recruitee.com",
    "eightfold.ai",
    "avature.net",
)

# Job-board marketing that matches keywords ("applying", "offer") but never carries a status.
# Matched by suffix, so list marketing subdomains only when the parent also sends real mail.
BLOCKED_SENDER_DOMAINS = (
    # Skipped entirely at the user's request (alerts, InMail and notifications outnumber the
    # application confirmations by far). This overrides the spec's ATS list.
    "linkedin.com",
    "hey.simplify.jobs",
    "billing.simplify.jobs",
    "bb3.wayup.com",
    "optimhire.com",
    "optimhire.ai",
    "tsenta.com",
)

KEYWORD_RE = re.compile(
    r"\b(applications?|applied|applying|interview(?:s|ing|ed)?|unfortunately|next steps?"
    r"|assessments?|offers?|candidacy|thank(?:s| you) for your interest|received your resume)\b",
    re.IGNORECASE,
)

# The same rules as a Gmail search, so first scans ask Gmail only for likely job mail instead of
# fetching every message. Gmail searches whole bodies, so this finds a superset of what the
# local subject/snippet check passes; the local check still runs on every result.
SEARCH_KEYWORDS = (
    "application",
    "applications",
    "applied",
    "applying",
    "interview",
    "interviews",
    "interviewing",
    "interviewed",
    "unfortunately",
    '"next step"',
    '"next steps"',
    "assessment",
    "assessments",
    "offer",
    "offers",
    "candidacy",
    '"thank you for your interest"',
    '"thanks for your interest"',
    '"received your resume"',
)


def gmail_search_query() -> str:
    terms = [f"from:{d}" for d in ATS_DOMAINS] + list(SEARCH_KEYWORDS)
    excluded = " ".join(f"-from:{d}" for d in BLOCKED_SENDER_DOMAINS)
    return "{" + " ".join(terms) + "} " + excluded


# Our own outgoing mail and junk never describe an application status.
DROP_LABELS = frozenset({"SENT", "DRAFT", "SPAM", "TRASH", "CHAT"})


@dataclass(frozen=True)
class PrefilterDecision:
    passed: bool
    reason: str


def _match_domain(domain: str, candidates: Iterable[str]) -> str | None:
    domain = domain.lower()
    for candidate in candidates:
        if domain == candidate or domain.endswith("." + candidate):
            return candidate
    return None


def match_ats_domain(domain: str) -> str | None:
    return _match_domain(domain, ATS_DOMAINS)


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
    if blocked := _match_domain(msg.sender_domain, BLOCKED_SENDER_DOMAINS):
        return PrefilterDecision(False, f"blocked:{blocked}")
    if ats := match_ats_domain(msg.sender_domain):
        return PrefilterDecision(True, f"ats:{ats}")
    if m := KEYWORD_RE.search(f"{msg.subject}\n{msg.snippet}"):
        return PrefilterDecision(True, f"keyword:{m.group(1).lower()}")
    return PrefilterDecision(False, "no_match")
