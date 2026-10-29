"""Notification types. Telegram (step 5) and Slack (step 9) implement Notifier."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InterviewAlert:
    application_id: int
    company: str
    role: str | None
    scheduled_at: datetime | None
    meeting_link: str | None
    rescheduled: bool = False


class Notifier(Protocol):
    def interview_alert(self, alert: InterviewAlert) -> None: ...


def build_notifier(settings) -> "Notifier":
    """Slack when SLACK_WEBHOOK_URL is set, otherwise log-only."""
    if settings.slack_webhook_url is not None:
        from app.notify.slack import SlackNotifier

        return SlackNotifier(
            settings.slack_webhook_url.get_secret_value(), timezone=settings.timezone
        )
    log.warning("SLACK_WEBHOOK_URL is empty; interview alerts are only logged")
    return LogNotifier()


class LogNotifier:
    """Fallback when no channel is configured."""

    def outreach_reply(self, reply) -> None:
        log.info(
            "Cold email %s: %s", "bounced" if reply.bounced else "reply", reply.outreach_email_id
        )

    def interview_alert(self, alert: InterviewAlert) -> None:
        log.info(
            "Interview %s: %s / %s at %s",
            "rescheduled" if alert.rescheduled else "invite",
            alert.company,
            alert.role or "?",
            alert.scheduled_at.isoformat() if alert.scheduled_at else "time TBD",
        )
