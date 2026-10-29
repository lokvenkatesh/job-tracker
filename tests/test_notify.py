import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.db.models import Application, Email, Interview, StatusEvent
from app.notify import digest as digest_mod
from app.notify.base import InterviewAlert, LogNotifier, build_notifier
from app.notify.digest import build_digest, digest_message, digest_text
from app.notify.slack import SlackError, SlackNotifier, format_when, interview_alert_message
from app.pipeline import scheduler

HOOK = "https://hooks.slack.com/services/T000/B000/secretsecret"
NOW = datetime(2026, 10, 29, 23, 59, tzinfo=UTC)  # 7:59 PM New York
TZ = "America/New_York"


def notifier_with(responses):
    """responses: list of httpx.Response or Exception, consumed in order."""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SlackNotifier(HOOK, timezone=TZ, client=client, sleep=lambda s: None), calls


# --- Slack client ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://hooks.slack.com/x", "https://evil.example/hooks.slack.com/", "hooks.slack.com/x"],
)
def test_rejects_non_slack_urls(url):
    with pytest.raises(ValueError):
        SlackNotifier(url, timezone=TZ)


def test_post_success():
    n, calls = notifier_with([httpx.Response(200, text="ok")])
    n.post({"text": "hi"})
    assert calls == [{"text": "hi"}]


def test_retries_rate_limit_and_server_errors():
    n, calls = notifier_with(
        [
            httpx.Response(429, headers={"retry-after": "1"}),
            httpx.Response(503),
            httpx.Response(200),
        ]
    )
    n.post({"text": "hi"})
    assert len(calls) == 3


def test_gives_up_after_retries():
    n, _ = notifier_with([httpx.Response(500)] * 3)
    with pytest.raises(SlackError, match="500"):
        n.post({"text": "hi"})


def test_client_error_is_not_retried():
    n, calls = notifier_with([httpx.Response(400, text="invalid_payload")])
    with pytest.raises(SlackError, match="invalid_payload"):
        n.post({"text": "hi"})
    assert len(calls) == 1


def test_network_error_never_leaks_webhook_url():
    n, _ = notifier_with([httpx.ConnectError(f"cannot reach {HOOK}")] * 3)
    with pytest.raises(SlackError) as e:
        n.post({"text": "hi"})
    assert "secretsecret" not in str(e.value) and e.value.__cause__ is None


def test_build_notifier():
    assert isinstance(build_notifier(Settings(_env_file=None)), LogNotifier)
    n = build_notifier(Settings(_env_file=None, slack_webhook_url=HOOK))
    assert isinstance(n, SlackNotifier)


# --- interview alert format -------------------------------------------------


def alert(**kw):
    base = dict(
        application_id=1,
        company="Acme",
        role="ML Engineer",
        scheduled_at=datetime(2026, 11, 3, 19, 0, tzinfo=UTC),
        meeting_link="https://zoom.us/j/123",
    )
    base.update(kw)
    return InterviewAlert(**base)


def test_alert_message_contents():
    m = interview_alert_message(alert(), TZ)
    assert m["blocks"][0]["text"]["text"] == "Interview invite: Acme"
    fields = [f["text"] for f in m["blocks"][1]["fields"]]
    assert fields[0] == "*Role*\nML Engineer"
    assert fields[1] == "*When*\nTue Nov 3, 2:00 PM EST"
    assert fields[2] == "*Link*\n<https://zoom.us/j/123|Join>"
    assert "Tue Nov 3, 2:00 PM EST" in m["text"]


def test_alert_rescheduled_without_time_or_link():
    m = interview_alert_message(
        alert(rescheduled=True, scheduled_at=None, meeting_link=None, role=None), TZ
    )
    assert m["blocks"][0]["text"]["text"] == "Interview rescheduled: Acme"
    fields = [f["text"] for f in m["blocks"][1]["fields"]]
    assert fields == ["*Role*\nNot stated", "*When*\nNot scheduled yet (check the email)"]


def test_alert_escapes_mrkdwn_and_links():
    m = interview_alert_message(alert(role="R&D <Lead>", meeting_link="https://x.test/a|b>c"), TZ)
    fields = [f["text"] for f in m["blocks"][1]["fields"]]
    assert fields[0] == "*Role*\nR&amp;D &lt;Lead&gt;"
    assert fields[2] == "*Link*\n<https://x.test/a%7Cb%3Ec|Join>"


def test_format_when_daylight_saving():
    assert format_when(datetime(2026, 7, 1, 16, tzinfo=UTC), TZ) == "Wed Jul 1, 12:00 PM EDT"


def test_slack_notifier_sends_alert():
    n, calls = notifier_with([httpx.Response(200)])
    n.interview_alert(alert())
    assert calls[0]["blocks"][0]["text"]["text"] == "Interview invite: Acme"


# --- digest ----------------------------------------------------------------


def _app(s, company, role, status, at=NOW - timedelta(days=30)):
    a = Application(
        company=company, role=role, status=status, source="t", last_update=at, status_changed_at=at
    )
    s.add(a)
    s.flush()
    return a


def _email(s, gid, at):
    e = Email(
        gmail_id=gid,
        thread_id=gid,
        type="x",
        received_at=at,
        confidence=1,
        sender_email="x@y.z",
        subject="s",
    )
    s.add(e)
    s.flush()
    return e


def _event(s, app, old, new, *, email_at=None, created_at=None):
    e = _email(s, f"g{app.id}{new}", email_at) if email_at else None
    ev = StatusEvent(
        application_id=app.id, old_status=old, new_status=new, email_id=e.id if e else None
    )
    if created_at:
        ev.created_at = created_at
    s.add(ev)


def test_build_digest(session):
    recent, old = NOW - timedelta(hours=3), NOW - timedelta(days=3)
    new = _app(session, "NewCo", "SWE", "applied")
    _event(session, new, None, "applied", email_at=recent)
    backfilled = _app(session, "OldCo", "SWE", "applied")
    _event(session, backfilled, None, "applied", email_at=old, created_at=recent)  # processed today
    rej = _app(session, "RejCo", "DS", "rejected")
    _event(session, rej, "applied", "rejected", email_at=recent)
    straight_rej = _app(session, "GhostCo", None, "rejected")
    _event(session, straight_rej, None, "rejected", email_at=recent)
    adv = _app(session, "UpCo", "MLE", "interview")
    _event(session, adv, "applied", "interview", email_at=recent)
    manual = _app(session, "ManualCo", "PM", "offer")
    _event(session, manual, "interview", "offer", created_at=recent)
    for app, when, link in [
        (adv, NOW + timedelta(days=2), "https://zoom.us/j/1"),
        (adv, None, None),
        (rej, NOW + timedelta(days=1), None),  # rejected app: hidden
        (manual, NOW + timedelta(days=10), None),  # beyond a week
        (manual, NOW - timedelta(days=1), None),  # past
    ]:
        session.add(
            Interview(
                application_id=app.id,
                scheduled_at=when,
                meeting_link=link,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    flagged = _email(session, "review", recent)
    flagged.needs_review = True
    session.flush()

    d = build_digest(session, now=NOW)
    assert [e.company for e in d.new_applications] == ["NewCo"]
    assert {e.company for e in d.rejections} == {"RejCo", "GhostCo"}
    assert {(e.company, e.status.value) for e in d.advanced} == {
        ("UpCo", "interview"),
        ("ManualCo", "offer"),
    }
    assert [(i.company, i.scheduled_at is None) for i in d.upcoming] == [
        ("UpCo", False),
        ("UpCo", True),
    ]
    assert d.needs_review == 1 and d.total_applications == 6
    assert d.response_rate == pytest.approx(4 / 6)
    assert not d.quiet

    text = digest_text(d, TZ)
    assert text.startswith("Job search digest — Thu Oct 29")
    assert "Moved forward (2)" in text and "• *UpCo* — MLE → interview" in text
    assert "Sat Oct 31, 7:59 PM EDT — *UpCo* (MLE) · <https://zoom.us/j/1|Join>" in text
    assert "*1 waiting for review*" in text


def test_quiet_digest(session):
    d = build_digest(session, now=NOW)
    assert d.quiet
    m = digest_message(d, TZ)
    assert "No changes in the last 24 hours." in digest_text(d, TZ)
    assert m["text"].endswith("0 new, 0 rejections, 0 moved forward, 0 upcoming interviews")


def test_digest_truncates_long_lists(session, monkeypatch):
    monkeypatch.setattr(digest_mod, "MAX_LISTED", 3)
    for i in range(5):
        a = _app(session, f"Co{i}", None, "applied")
        _event(session, a, None, "applied", email_at=NOW - timedelta(hours=1))
    text = digest_text(build_digest(session, now=NOW), TZ)
    assert "New applications (5)" in text and "…and 2 more" in text and "Co3" not in text


# --- scheduling ------------------------------------------------------------


def test_scheduler_registers_tracker_and_digest(tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    s = Settings(
        _env_file=None,
        database_url="sqlite://",
        anthropic_api_key="k",
        gmail_readonly_token_file=token,
        digest_hour=20,
        timezone=TZ,
    )
    sched = scheduler.start_scheduler(s)
    try:
        jobs = {j.id: j for j in sched.get_jobs()}
        assert set(jobs) == {"tracker", "digest"}
        assert str(jobs["digest"].trigger) == "cron[hour='20', minute='0']"
        assert str(jobs["digest"].trigger.timezone) == TZ
    finally:
        sched.shutdown(wait=False)


def test_digest_job_skips_without_slack(caplog):
    scheduler.run_digest_job(Settings(_env_file=None))
    assert "Daily digest skipped" in caplog.text
