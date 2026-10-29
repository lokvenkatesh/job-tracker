"""Classification result type and the defensive parser for Claude's JSON output."""

import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from dateutil import parser as date_parser
from pydantic import BaseModel


class EmailType(StrEnum):
    APPLICATION_CONFIRMATION = "application_confirmation"
    REJECTION = "rejection"
    INTERVIEW_INVITE = "interview_invite"
    ASSESSMENT = "assessment"
    OFFER = "offer"
    RECRUITER_OUTREACH = "recruiter_outreach"
    OTHER = "other"


_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

# Sent as output_config.format so the API guarantees JSON of this shape.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": [t.value for t in EmailType]},
        "company": _NULLABLE_STRING,
        "role": _NULLABLE_STRING,
        "interview_datetime": _NULLABLE_STRING,
        "meeting_link": _NULLABLE_STRING,
        "confidence": {"type": "number"},
    },
    "required": ["type", "company", "role", "interview_datetime", "meeting_link", "confidence"],
    "additionalProperties": False,
}


class Classification(BaseModel):
    type: EmailType
    company: str | None = None
    role: str | None = None
    interview_datetime: datetime | None = None  # timezone-aware when set
    meeting_link: str | None = None
    confidence: float = 0.0
    error: str | None = None  # set when the result is a fallback, not a real classification

    def needs_review(self, threshold: float) -> bool:
        return self.error is not None or self.confidence < threshold


class ClassificationParseError(ValueError):
    pass


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _parse_datetime(value: Any) -> datetime | None:
    """Accept ISO 8601 only when it carries a timezone; a naive time is ambiguous."""
    text = _clean_str(value)
    if text is None:
        return None
    try:
        parsed = date_parser.isoparse(text)
    except (ValueError, OverflowError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _parse_link(value: Any) -> str | None:
    text = _clean_str(value)
    if text and text.startswith(("https://", "http://")) and " " not in text:
        return text
    return None


def parse_classification(raw: str | dict[str, Any]) -> Classification:
    """Validate Claude's output. Raises ClassificationParseError for anything unusable;
    normalizes the rest (blank strings -> None, confidence clamped, bad dates/links dropped)."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ClassificationParseError(f"invalid JSON: {e.msg}") from e
    else:
        data = raw
    if not isinstance(data, dict):
        raise ClassificationParseError("expected a JSON object")

    try:
        email_type = EmailType(data.get("type"))
    except ValueError as e:
        raise ClassificationParseError(f"unknown type {data.get('type')!r}") from e

    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise ClassificationParseError("confidence must be a number")

    is_interview = email_type is EmailType.INTERVIEW_INVITE
    return Classification(
        type=email_type,
        company=_clean_str(data.get("company")),
        role=_clean_str(data.get("role")),
        interview_datetime=_parse_datetime(data.get("interview_datetime"))
        if is_interview
        else None,
        meeting_link=_parse_link(data.get("meeting_link")) if is_interview else None,
        confidence=min(max(float(confidence), 0.0), 1.0),
    )


def failed(error: str) -> Classification:
    """Result for an email we could not classify; always lands in the review queue."""
    return Classification(type=EmailType.OTHER, confidence=0.0, error=error)
