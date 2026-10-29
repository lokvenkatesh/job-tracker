"""Slack incoming-webhook notifier. The webhook URL is a secret: it's never logged, and only
https://hooks.slack.com/ URLs are accepted so a typo can't send data elsewhere."""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.notify.base import InterviewAlert

log = logging.getLogger(__name__)

WEBHOOK_PREFIX = "https://hooks.slack.com/"
RETRIES = 3


class SlackError(RuntimeError):
    pass


def escape(text: str) -> str:
    """Slack mrkdwn treats &, <, > as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_when(dt: datetime | None, timezone: str) -> str:
    if dt is None:
        return "Not scheduled yet (check the email)"
    local = dt.astimezone(ZoneInfo(timezone))
    return f"{local:%a %b} {local.day}, {local:%-I:%M %p %Z}"


def link(url: str, label: str) -> str:
    return f"<{url.replace('>', '%3E').replace('|', '%7C')}|{escape(label)}>"


def interview_alert_message(alert: InterviewAlert, timezone: str) -> dict:
    verb = "Interview rescheduled" if alert.rescheduled else "Interview invite"
    title = f"{verb}: {alert.company}"
    fields = [
        {"type": "mrkdwn", "text": f"*Role*\n{escape(alert.role or 'Not stated')}"},
        {"type": "mrkdwn", "text": f"*When*\n{format_when(alert.scheduled_at, timezone)}"},
    ]
    if alert.meeting_link:
        fields.append({"type": "mrkdwn", "text": f"*Link*\n{link(alert.meeting_link, 'Join')}"})
    return {
        "text": f"{title} ({format_when(alert.scheduled_at, timezone)})",  # notification fallback
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
            {"type": "section", "fields": fields},
        ],
    }


class SlackNotifier:
    def __init__(
        self,
        webhook_url: str,
        *,
        timezone: str,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        if not webhook_url.startswith(WEBHOOK_PREFIX):
            raise ValueError("SLACK_WEBHOOK_URL must start with https://hooks.slack.com/")
        self._url = webhook_url
        self.timezone = timezone
        self._client = client or httpx.Client(timeout=10)
        self._sleep = sleep

    def post(self, message: dict) -> None:
        for attempt in range(1, RETRIES + 1):
            try:
                r = self._client.post(self._url, json=message)
            except httpx.TransportError as e:
                if attempt == RETRIES:
                    raise SlackError(f"Slack unreachable: {type(e).__name__}") from None
                self._sleep(2**attempt)
                continue
            if r.status_code == 200:
                return
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < RETRIES:
                    self._sleep(float(r.headers.get("retry-after", 2**attempt)))
                    continue
            # Slack returns short plain-text reasons like "invalid_payload" or "no_service".
            raise SlackError(f"Slack webhook returned {r.status_code}: {r.text[:100]}")

    def outreach_reply(self, reply) -> None:
        if reply.bounced:
            text = f"Cold email bounced: {escape(reply.recipient)} ({escape(reply.subject)})"
        else:
            text = (
                f"Reply to your cold email from {escape(reply.recipient)}: {escape(reply.subject)}"
            )
        self.post({"text": text})

    def interview_alert(self, alert: InterviewAlert) -> None:
        self.post(interview_alert_message(alert, self.timezone))
        log.info("Sent Slack interview alert for application %s", alert.application_id)
