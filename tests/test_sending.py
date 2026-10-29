import base64
import email
import random
from datetime import UTC, date, datetime, timedelta
from email import policy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db.models import OutreachCampaign, OutreachEmail, Resume, SyncStateRow
from app.gmail.message import EmailMessage
from app.main import create_app
from app.notify.digest import build_digest, digest_text
from app.outreach import service
from app.outreach.followups import business_days_between, create_followups, due_for_followup
from app.outreach.generator import Draft, DraftGenerator
from app.outreach.replies import record_reply
from app.outreach.sender import build_message, send_next, sent_today
from tests.fake_gmail import http_error
from tests.test_api import PASSWORD, settings
from tests.test_outreach import FakeGenerator, ScriptedAnthropic, good_body

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # Monday 10:00 New York
TZ = "America/New_York"


def cfg(**kw):
    base = dict(
        _env_file=None,
        timezone=TZ,
        outreach_daily_cap=30,
        outreach_min_gap_seconds=60,
        outreach_max_gap_seconds=180,
        outreach_dedupe_days=30,
        followup_business_days=5,
    )
    base.update(kw)
    return Settings(**base)


class FakeSend:
    """Stands in for both the gmail.send and gmail.readonly services."""

    def __init__(self, *, fail=None, in_sent=None):
        self.sent = []
        self.searches = []
        self.fail = list(fail or [])
        self.in_sent = in_sent or {}

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        def run():
            if self.fail:
                raise self.fail.pop(0)
            n = len(self.sent) + 1
            self.sent.append(body)
            return {"id": f"m{n}", "threadId": body.get("threadId", f"t{n}")}

        return _Exec(run)

    def get(self, userId, id, format, metadataHeaders):
        return _Exec(
            lambda: {
                "payload": {"headers": [{"name": "Message-Id", "value": f"<{id}@mail.gmail.com>"}]}
            }
        )

    def list(self, userId, q, maxResults):
        self.searches.append(q)
        hit = next((v for k, v in self.in_sent.items() if k in q), None)
        return _Exec(lambda: {"messages": [hit]} if hit else {})


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


def parse(body) -> email.message.EmailMessage:
    return email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=policy.default)


def campaign(db, *, requested=True, resume=True):
    r = None
    if resume:
        r = Resume(
            filename="Lok Resume.pdf",
            parsed_text="x" * 300,
            content=b"%PDF-1.4 fake",
            content_type="application/pdf",
            is_default=True,
        )
        db.add(r)
        db.flush()
    c = OutreachCampaign(
        jd_text="jd",
        resume_id=r.id if r else None,
        brief={"company": "Globex"},
        send_requested_at=NOW - timedelta(minutes=5) if requested else None,
    )
    db.add(c)
    db.flush()
    return c


def outreach(db, c, addr, status="approved", **kw):
    e = OutreachEmail(
        campaign_id=c.id,
        recipient_email=addr,
        recipient_name=kw.pop("name", None),
        subject=kw.pop("subject", "ML Engineer at Globex"),
        body=kw.pop("body", "Hi,\n\nBody.\n\nLok"),
        status=status,
        generated_at=NOW - timedelta(hours=1),
        **kw,
    )
    db.add(e)
    db.flush()
    return e


def tick(db, gmail, settings_=None, now=NOW, seed=1):
    result = send_next(
        db,
        send_service=gmail,
        read_service=gmail,
        settings=settings_ or cfg(),
        now=now,
        rng=random.Random(seed),
    )
    db.commit()
    return result


# --- message building -----------------------------------------------------------


def test_message_has_one_recipient_and_optional_attachment(session):
    c = campaign(session)
    e = outreach(session, c, "priya@globex.com", attach_resume=True)
    resume = session.get(Resume, c.resume_id)
    m = build_message(e, resume=resume)
    assert m["To"] == "priya@globex.com" and m["Cc"] is None and m["Bcc"] is None
    assert m["Message-ID"] is None  # Gmail assigns its own
    files = [p.get_filename() for p in m.iter_attachments()]
    assert files == ["Lok Resume.pdf"]
    e.attach_resume = False
    assert list(build_message(e, resume=resume).iter_attachments()) == []


# --- sender gating ----------------------------------------------------------------


def test_sends_only_approved_in_requested_campaigns(session):
    c = campaign(session)
    other = campaign(session, requested=False)
    outreach(session, c, "draft@x.com", status="draft")
    outreach(session, c, "skipped@x.com", status="skipped")
    outreach(session, other, "not-requested@x.com")
    gmail = FakeSend()
    assert tick(session, gmail).action == "idle"
    assert gmail.sent == []
    ok = outreach(session, c, "ok@x.com")
    assert tick(session, gmail, now=NOW).action == "sent"
    assert [parse(b)["To"] for b in gmail.sent] == ["ok@x.com"]
    session.refresh(ok)
    assert (ok.status, ok.gmail_message_id, ok.thread_id, ok.sent_at) == ("sent", "m1", "t1", NOW)


def test_one_email_per_tick_with_random_gap(session):
    c = campaign(session)
    for i in range(3):
        outreach(session, c, f"p{i}@x.com")
    gmail = FakeSend()
    assert tick(session, gmail).action == "sent"
    next_at = session.get(SyncStateRow, 1).outreach_next_send_at
    assert 60 <= (next_at - NOW).total_seconds() <= 180
    assert tick(session, gmail, now=NOW + timedelta(seconds=59)).action == "waiting"
    assert len(gmail.sent) == 1
    assert tick(session, gmail, now=next_at).action == "sent"
    assert len(gmail.sent) == 2


def test_gap_varies(session):
    gaps = set()
    for seed in range(5):
        state = SyncStateRow(id=1)
        from app.outreach.sender import _schedule_next

        _schedule_next(state, NOW, cfg(), random.Random(seed))
        gaps.add(round((state.outreach_next_send_at - NOW).total_seconds()))
    assert len(gaps) > 1 and all(60 <= g <= 180 for g in gaps)


def test_daily_cap_resets_at_local_midnight(session):
    c = campaign(session)
    for i in range(3):
        outreach(session, c, f"p{i}@x.com")
    gmail, s = (
        FakeSend(),
        cfg(outreach_daily_cap=2, outreach_min_gap_seconds=0, outreach_max_gap_seconds=0),
    )
    assert tick(session, gmail, s).action == "sent"
    assert tick(session, gmail, s, now=NOW + timedelta(minutes=1)).action == "sent"
    assert tick(session, gmail, s, now=NOW + timedelta(minutes=2)).action == "cap_reached"
    assert sent_today(session, NOW + timedelta(minutes=2), TZ) == 2
    # 11:30 PM New York is still the same day; 12:30 AM is the next
    assert (
        tick(session, gmail, s, now=datetime(2026, 11, 3, 4, 30, tzinfo=UTC)).action
        == "cap_reached"
    )
    assert tick(session, gmail, s, now=datetime(2026, 11, 3, 5, 30, tzinfo=UTC)).action == "sent"


def test_recent_recipient_rechecked_at_send_time(session):
    c = campaign(session)
    outreach(session, c, "priya@globex.com", status="sent", sent_at=NOW - timedelta(days=3))
    dup = outreach(session, c, "priya@globex.com")
    gmail = FakeSend()
    assert tick(session, gmail).action == "skipped"
    assert gmail.sent == [] and dup.status == "skipped" and "not sent again" in dup.skip_reason
    override = outreach(session, c, "priya@globex.com", allow_recent=True)
    assert tick(session, gmail).action == "sent"
    session.refresh(override)
    assert override.status == "sent"


def test_interrupted_send_is_recovered_not_repeated(session):
    c = campaign(session)
    e = outreach(session, c, "p@x.com", send_attempts=1)  # attempt recorded, outcome lost
    gmail = FakeSend(in_sent={"to:p@x.com": {"id": "m9", "threadId": "t9"}})
    assert tick(session, gmail).action == "recovered"
    assert gmail.sent == []
    assert gmail.searches == ['in:sent to:p@x.com subject:"ML Engineer at Globex" newer_than:2d']
    session.refresh(e)
    assert (e.status, e.gmail_message_id, e.thread_id) == ("sent", "m9", "t9")
    assert e.message_id_header == "<m9@mail.gmail.com>"


def test_interrupted_send_not_found_is_sent(session):
    c = campaign(session)
    e = outreach(session, c, "p@x.com", send_attempts=1)
    gmail = FakeSend()
    assert tick(session, gmail).action == "sent"
    session.refresh(e)
    assert len(gmail.sent) == 1 and e.send_attempts == 2


def test_real_message_id_stored_after_send(session):
    c = campaign(session)
    e = outreach(session, c, "p@x.com")
    gmail = FakeSend()
    tick(session, gmail)
    session.refresh(e)
    assert e.message_id_header == "<m1@mail.gmail.com>" and e.send_attempts == 1


def test_failed_attempt_is_not_searched_again(session):
    c = campaign(session)
    outreach(session, c, "p@x.com")
    gmail = FakeSend(fail=[http_error(503)])
    s = cfg(outreach_min_gap_seconds=0, outreach_max_gap_seconds=0)
    tick(session, gmail, s)
    tick(session, gmail, s, now=NOW + timedelta(minutes=1))
    assert gmail.searches == [] and len(gmail.sent) == 1


def test_gmail_client_error_skips(session):
    c = campaign(session)
    e = outreach(session, c, "bad@x.com")
    gmail = FakeSend(fail=[http_error(400)])
    assert tick(session, gmail).action == "failed"
    session.refresh(e)
    assert e.status == "skipped" and "Gmail error 400" in e.skip_reason


def test_gmail_server_error_retries_then_gives_up(session):
    c = campaign(session)
    e = outreach(session, c, "p@x.com")
    gmail = FakeSend(fail=[http_error(503)] * 3)
    s = cfg(outreach_min_gap_seconds=0, outreach_max_gap_seconds=0)
    for i in range(3):
        assert tick(session, gmail, s, now=NOW + timedelta(minutes=i)).action == "failed"
        session.refresh(e)
        assert e.status == ("approved" if i < 2 else "skipped")
    assert e.send_attempts == 3


def test_followup_threads_and_skips_dedupe(session):
    c = campaign(session)
    first = outreach(
        session,
        c,
        "p@x.com",
        status="sent",
        sent_at=NOW - timedelta(days=8),
        thread_id="t-orig",
        message_id_header="<orig@job-tracker.local>",
    )
    fu = outreach(
        session, c, "p@x.com", subject="Re: ML Engineer at Globex", followup_of_id=first.id
    )
    gmail = FakeSend()
    assert tick(session, gmail).action == "sent"
    sent = gmail.sent[0]
    assert sent["threadId"] == "t-orig"
    m = parse(sent)
    assert (
        m["In-Reply-To"] == "<orig@job-tracker.local>"
        and m["Subject"] == "Re: ML Engineer at Globex"
    )
    session.refresh(fu)
    assert fu.thread_id == "t-orig"


# --- replies ----------------------------------------------------------------------


def inbound(sender, thread="t1", at=NOW + timedelta(days=1)):
    return EmailMessage(
        gmail_id="g",
        thread_id=thread,
        sender_name="",
        sender_email=sender,
        subject="Re: hi",
        snippet="",
        received_at=at,
    )


def test_reply_from_recipient_or_domain(session):
    c = campaign(session)
    e = outreach(session, c, "priya@globex.com", status="sent", sent_at=NOW, thread_id="t1")
    assert record_reply(session, inbound("someone@other.com")) is None
    alert = record_reply(session, inbound("assistant@globex.com"), application_id=None)
    assert alert and not alert.bounced and e.status == "replied" and e.replied_at
    assert record_reply(session, inbound("priya@globex.com")) is None  # already counted


def test_bounce_is_not_a_reply(session):
    c = campaign(session)
    e = outreach(session, c, "nobody@globex.com", status="sent", sent_at=NOW, thread_id="t1")
    alert = record_reply(session, inbound("mailer-daemon@googlemail.com"))
    assert alert.bounced and e.status == "sent" and e.send_error == "bounced"


def test_reply_marks_whole_thread_and_links_application(session):
    c = campaign(session)
    first = outreach(
        session, c, "p@x.com", status="sent", sent_at=NOW - timedelta(days=8), thread_id="t1"
    )
    fu = outreach(
        session, c, "p@x.com", status="sent", sent_at=NOW, thread_id="t1", followup_of_id=first.id
    )
    record_reply(session, inbound("p@x.com"), application_id=None)
    assert first.status == fu.status == "replied"


def test_tracker_marks_outreach_replied(session_factory):
    from app.pipeline.tracker import run_once
    from tests.fake_gmail import FakeGmail
    from tests.test_pipeline import RecordingNotifier, cls, deps_for, msg

    with session_factory() as s:
        c = campaign(s)
        outreach(
            s,
            c,
            "priya@globex.com",
            status="sent",
            sent_at=NOW - timedelta(days=1),
            thread_id="t-out",
        )
        s.commit()

    class Notifier(RecordingNotifier):
        replies = []

        def outreach_reply(self, reply):
            self.replies.append(reply)

    notifier = Notifier()
    gmail = FakeGmail(
        messages=[
            msg("r1", NOW, subject="Re: quick question", sender="priya@globex.com", thread="t-out")
        ]
    )
    run_once(deps_for(session_factory, gmail, {"r1": cls("other")}, notifier=notifier))
    with session_factory() as s:
        assert s.scalar(select(OutreachEmail.status)) == "replied"
    assert len(notifier.replies) == 1


# --- follow-ups ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (date(2026, 10, 30), date(2026, 11, 6), 5),  # Fri -> next Fri
        (date(2026, 11, 2), date(2026, 11, 6), 4),
        (date(2026, 10, 31), date(2026, 11, 1), 0),
    ],  # Sat -> Sun
)
def test_business_days(start, end, expected):
    assert business_days_between(start, end) == expected


def test_due_for_followup(session):
    c = campaign(session)
    due = outreach(session, c, "due@x.com", status="sent", sent_at=NOW - timedelta(days=8))
    outreach(session, c, "early@x.com", status="sent", sent_at=NOW - timedelta(days=3))
    outreach(session, c, "replied@x.com", status="replied", sent_at=NOW - timedelta(days=9))
    outreach(
        session,
        c,
        "bounced@x.com",
        status="sent",
        sent_at=NOW - timedelta(days=9),
        send_error="bounced",
    )
    has_fu = outreach(session, c, "has@x.com", status="sent", sent_at=NOW - timedelta(days=9))
    outreach(session, c, "has@x.com", status="draft", followup_of_id=has_fu.id)
    got = due_for_followup(session, now=NOW, timezone=TZ, business_days=5)
    assert [e.id for e in got] == [due.id]


class FollowupGen(FakeGenerator):
    def followup(
        self, brief, recipient, *, original_subject, original_body, sources, instructions=None
    ):
        self.calls.append(("followup", recipient.email, instructions))
        return Draft(
            subject=f"Re: {original_subject}",
            body="Hi there,\n\n" + "short bump " * 25 + "\n\nLok",
            issues=[],
        )


def test_create_followups(session_factory):
    with session_factory() as s:
        c = campaign(s)
        outreach(
            s, c, "due@x.com", status="sent", sent_at=NOW - timedelta(days=8), attach_resume=True
        )
        s.commit()
    gen = FollowupGen()
    assert create_followups(session_factory, gen, timezone=TZ, business_days=5, now=NOW) == 1
    assert (
        create_followups(session_factory, gen, timezone=TZ, business_days=5, now=NOW) == 0
    )  # once only
    with session_factory() as s:
        fu = s.scalar(select(OutreachEmail).where(OutreachEmail.followup_of_id.is_not(None)))
        assert (fu.status, fu.subject, fu.attach_resume, fu.allow_recent) == (
            "draft",
            "Re: ML Engineer at Globex",
            False,
            True,
        )
        assert fu.generated_at is not None
        service.regenerate(s, fu, gen, "warmer")
    assert gen.calls[-1] == ("followup", "due@x.com", "warmer")


def test_generator_followup_forces_subject_and_length():
    good = {
        "subject": "whatever",
        "body": "Hi Priya,\n\n"
        + "I wanted to bring my note about the ML role back to the top. " * 4
        + "\n\nLok",
    }
    client = ScriptedAnthropic([good])
    g = DraftGenerator(client, settings(), model="claude-opus-5-5")
    from app.outreach.recipients import Recipient

    d = g.followup(
        {},
        Recipient("p@x.com", "Priya Shah"),
        original_subject="ML Engineer at Globex",
        original_body=good_body(),
        sources="",
    )
    assert d.subject == "Re: ML Engineer at Globex" and d.issues == []
    assert "<original_email>" in client.requests[0]["messages"][0]["content"]


# --- API + pages --------------------------------------------------------------------


@pytest.fixture
def client(session_factory, tmp_path):
    token = tmp_path / "token_send.json"
    app = create_app(settings(gmail_send_token_file=token), session_factory)
    app.state.draft_generator = FakeGenerator()
    c = TestClient(app, follow_redirects=False)
    c.post("/auth/login", data={"username": "me", "password": PASSWORD})
    c.token = token
    return c


def _seed(session_factory, statuses):
    with session_factory() as s:
        c = campaign(s, requested=False)
        ids = [outreach(s, c, f"p{i}@x.com", status=st).id for i, st in enumerate(statuses)]
        s.commit()
        return c.id, ids


def test_send_requires_an_approved_email(client, session_factory):
    cid, _ = _seed(session_factory, ["draft"])
    r = client.post(f"/outreach/campaigns/{cid}/send")
    assert r.status_code == 409 and "Approve at least one" in r.json()["detail"]


def test_send_and_pause(client, session_factory):
    cid, _ = _seed(session_factory, ["approved", "approved", "draft"])
    r = client.post(f"/outreach/campaigns/{cid}/send").json()
    assert (
        r["sending"]
        and r["queued"] == 2
        and r["remaining_today"] == 30
        and not r["gmail_send_connected"]
    )
    client.token.write_text("{}")
    assert client.post(f"/outreach/campaigns/{cid}/send").json()["gmail_send_connected"]
    r = client.post(f"/outreach/campaigns/{cid}/pause").json()
    assert not r["sending"]
    with session_factory() as s:
        assert s.get(OutreachCampaign, cid).send_requested_at is None


def test_sent_history(client, session_factory):
    with session_factory() as s:
        c = campaign(s)
        outreach(s, c, "a@x.com", status="sent", sent_at=NOW - timedelta(days=2))
        outreach(s, c, "b@x.com", status="replied", sent_at=NOW - timedelta(days=1), replied_at=NOW)
        outreach(s, c, "c@x.com", status="approved")
        s.commit()
    rows = client.get("/outreach/sent").json()
    assert [(r["recipient_email"], r["status"]) for r in rows] == [
        ("b@x.com", "replied"),
        ("a@x.com", "sent"),
    ]
    html = client.get("/dashboard/outreach").text
    assert "Sent history" in html and "Replied" in html and "No reply yet" in html


def test_campaign_page_send_controls(client, session_factory):
    cid, _ = _seed(session_factory, ["approved", "approved"])
    page = client.get(f"/dashboard/outreach/{cid}").text
    assert "Send 2 approved" in page and "hx-confirm=" in page and "gmail_auth send" in page
    hx = {"HX-Request": "true"}
    r = client.post(f"/dashboard/outreach/{cid}/send", headers=hx)
    assert (
        "Pause sending" in r.text
        and "Sending 2 approved emails" in r.text
        and "every 10s" in r.text
    )
    r = client.post(f"/dashboard/outreach/{cid}/pause", headers=hx)
    assert "Send 2 approved" in r.text


def test_sender_job_without_database_is_noop():
    from app.pipeline.scheduler import run_sender_job

    assert run_sender_job(Settings(_env_file=None)) is None


# --- digest -------------------------------------------------------------------------


def test_digest_lists_replies_and_followups(session):
    c = campaign(session)
    outreach(
        session,
        c,
        "p@x.com",
        name="Priya Shah",
        recipient_company="Globex",
        status="replied",
        sent_at=NOW - timedelta(days=2),
        replied_at=NOW - timedelta(hours=2),
    )
    first = outreach(session, c, "q@x.com", status="sent", sent_at=NOW - timedelta(days=9))
    outreach(session, c, "q@x.com", status="draft", followup_of_id=first.id)
    text = digest_text(build_digest(session, now=NOW), TZ)
    assert "Replies to your cold emails (1)" in text and "Priya Shah (Globex)" in text
    assert "1 follow-up draft ready" in text
