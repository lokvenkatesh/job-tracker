"""Application status state machine.

Applied -> Assessment -> Interview -> Offer, with Rejected reachable from any state.
- Status never moves backward, and an email older than the last status change never moves it.
- Activity after a rejection (a new interview or offer) is surprising: flag for review, don't guess.
"""

from dataclasses import dataclass
from datetime import datetime

from app.classifier.schema import EmailType
from app.db.models import Status

TYPE_TO_STATUS: dict[EmailType, Status] = {
    EmailType.APPLICATION_CONFIRMATION: Status.APPLIED,
    EmailType.ASSESSMENT: Status.ASSESSMENT,
    EmailType.INTERVIEW_INVITE: Status.INTERVIEW,
    EmailType.OFFER: Status.OFFER,
    EmailType.REJECTION: Status.REJECTED,
}

_RANK = {Status.APPLIED: 0, Status.ASSESSMENT: 1, Status.INTERVIEW: 2, Status.OFFER: 3}


@dataclass(frozen=True)
class Transition:
    new_status: Status | None  # None = leave status unchanged
    reason: str
    needs_review: bool = False


def transition(
    current: Status | None,
    target: Status,
    *,
    email_at: datetime,
    status_changed_at: datetime | None,
) -> Transition:
    if current is None:
        return Transition(target, "created")
    if status_changed_at is not None and email_at < status_changed_at:
        return Transition(None, "older_email")
    if target == current:
        return Transition(None, "same_status")
    if target is Status.REJECTED:
        return Transition(Status.REJECTED, "rejected")
    if current is Status.REJECTED:
        return Transition(None, "activity_after_rejection", needs_review=True)
    if _RANK[target] < _RANK[current]:
        return Transition(None, "backward")
    return Transition(target, "advanced")
