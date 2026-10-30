from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.classifier.schema import Classification, EmailType, failed
from app.config import Settings
from app.db.models import Application, Email, Interview, StatusEvent
from app.gmail.fetcher import GmailFetcher
from app.gmail.sync_state import InMemorySyncStateStore, SyncState
from app.pipeline.scheduler import missing_config, start_scheduler
from app.pipeline.tracker import TrackerDeps, run_once
from tests.fake_gmail import FakeGmail, make_message

T0 = datetime(2026, 10, 20, 12, tzinfo=UTC)
MS = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731


def msg(gmail_id, at, *, subject="Your application", sender="x@ashbyhq.com", thread=None):
    return make_message(
        gmail_id,
        thread_id=thread,
        sender=sender,
        subject=subject,
        internal_ms=MS(at),
        body=f"body of {gmail_id}",
    )


def cls(type_, company=None, role=None, confidence=0.95, **kw):
    return Classification(
        type=EmailType(type_), company=company, role=role, confidence=confidence, **kw
    )


class ScriptedClassifier:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def classify(self, m):
        self.calls.append(m.gmail_id)
        r = self.results[m.gmail_id]
        if isinstance(r, Exception):
            raise r
        return r


class RecordingNotifier:
    def __init__(self, fail=False):
        self.alerts = []
        self.fail = fail

    def interview_alert(self, alert):
        if self.fail:
            raise RuntimeError("telegram down")
        self.alerts.append(alert)


class FakeEmbedder:
    def embed(self, text):
        return [0.0] * 384


def deps_for(session_factory, gmail, results, *, store=None, notifier=None):
    return TrackerDeps(
        fetcher=GmailFetcher(gmail, initial_backfill_days=30, now=lambda: T0 + timedelta(days=5)),
        classifier=ScriptedClassifier(results),
        session_factory=session_factory,
        sync_store=store or InMemorySyncStateStore(),
        notifier=notifier or RecordingNotifier(),
        settings=Settings(_env_file=None, timezone="America/New_York"),
        embedder=FakeEmbedder(),
    )


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def apps(session):
    return {a.company: a for a in session.scalars(select(Application))}


def test_full_lifecycle_and_idempotency(session_factory, session):
    gmail = FakeGmail(
        messages=[
            msg("c1", T0, subject="Thanks for applying to Acme"),
            msg("i1", T0 + timedelta(days=2), subject="Interview with Acme"),
            msg("noise", T0 + timedelta(days=1), subject="Weekly newsletter", sender="news@x.com"),
            msg("r1", T0 + timedelta(days=4), subject="Update on your application"),
        ],
        profile_history_id="500",
    )
    when = datetime(2026, 10, 27, 14, tzinfo=UTC)
    results = {
        "c1": cls("application_confirmation", "Acme", "ML Engineer"),
        "i1": cls(
            "interview_invite",
            "Acme Inc.",
            "ML Engineer",
            interview_datetime=when,
            meeting_link="https://zoom.us/j/1",
        ),
        "r1": cls("rejection", "Acme", None),
    }
    store = InMemorySyncStateStore()
    notifier = RecordingNotifier()
    deps = deps_for(session_factory, gmail, results, store=store, notifier=notifier)

    summary = run_once(deps, now=T0 + timedelta(days=5))

    assert summary.mode == "bootstrap" and summary.fetched == 4 and summary.queued == 3
    assert deps.classifier.calls == ["c1", "i1", "r1"]  # oldest first, noise never classified
    assert store.load().last_history_id == "500"
    acme = apps(session)["Acme"]
    assert acme.status == "rejected"
    assert acme.applied_date.isoformat() == "2026-10-20"
    events = session.scalars(select(StatusEvent).order_by(StatusEvent.id)).all()
    assert [(e.old_status, e.new_status) for e in events] == [
        (None, "applied"),
        ("applied", "interview"),
        ("interview", "rejected"),
    ]
    assert len(notifier.alerts) == 1 and notifier.alerts[0].scheduled_at == when
    assert summary.alerts_sent == 1

    # Re-processing the same gmail_ids (cursor lost) creates nothing new and skips Claude.
    deps2 = deps_for(session_factory, gmail, results)
    summary2 = run_once(deps2, now=T0 + timedelta(days=6))
    assert deps2.classifier.calls == []
    assert summary2.queued == 0
    assert count(session, Email) == 3
    assert count(session, Application) == 1
    assert count(session, StatusEvent) == 3


def test_incremental_run_uses_history(session_factory, session):
    gmail = FakeGmail(
        messages=[msg("c2", T0, subject="Thanks for applying")],
        history_pages=[
            {"history": [{"messagesAdded": [{"message": {"id": "c2"}}]}], "historyId": "9"}
        ],
    )
    store = InMemorySyncStateStore(SyncState(last_history_id="8", last_run_at=T0))
    summary = run_once(
        deps_for(
            session_factory, gmail, {"c2": cls("application_confirmation", "Globex")}, store=store
        )
    )
    assert summary.mode == "incremental"
    assert store.load().last_history_id == "9"
    assert "Globex" in apps(session)


def test_low_confidence_goes_to_review(session_factory, session):
    gmail = FakeGmail(messages=[msg("a", T0)])
    run_once(deps_for(session_factory, gmail, {"a": cls("rejection", "Acme", confidence=0.6)}))
    email = session.scalar(select(Email))
    assert email.needs_review and email.review_reason == "low_confidence"
    assert count(session, Application) == 0


def test_classifier_failure_result_goes_to_review(session_factory, session):
    gmail = FakeGmail(messages=[msg("a", T0)])
    run_once(deps_for(session_factory, gmail, {"a": failed("refusal")}))
    email = session.scalar(select(Email))
    assert email.needs_review and email.review_reason == "error:refusal"


def test_recruiter_outreach_is_not_an_application(session_factory, session):
    gmail = FakeGmail(messages=[msg("a", T0, subject="ML role at Stripe? interview")])
    run_once(deps_for(session_factory, gmail, {"a": cls("recruiter_outreach", "Stripe", "MLE")}))
    email = session.scalar(select(Email))
    assert email.type == "recruiter_outreach" and email.application_id is None
    assert count(session, Application) == 0


def test_status_never_moves_backward_from_older_email(session_factory, session):
    gmail = FakeGmail(
        messages=[
            msg("int", T0 + timedelta(days=3), subject="Interview"),
            msg("conf", T0, subject="Thanks for applying"),
        ]
    )
    # Delivered newest-first in a single batch is sorted; also simulate a late older email:
    run_once(
        deps_for(
            session_factory,
            gmail,
            {
                "int": cls("interview_invite", "Acme", "SWE"),
                "conf": cls("application_confirmation", "Acme", "SWE"),
            },
        )
    )
    assert apps(session)["Acme"].status == "interview"

    late = FakeGmail(messages=[msg("old-assess", T0 + timedelta(days=1), subject="Assessment")])
    run_once(deps_for(session_factory, late, {"old-assess": cls("assessment", "Acme", "SWE")}))
    session.expire_all()
    assert apps(session)["Acme"].status == "interview"


def test_reschedule_updates_existing_interview(session_factory, session):
    first = datetime(2026, 11, 3, 19, tzinfo=UTC)
    moved = datetime(2026, 11, 5, 19, tzinfo=UTC)
    gmail = FakeGmail(
        messages=[
            msg("i1", T0, subject="Interview scheduled"),
            msg("i2", T0 + timedelta(days=1), subject="Interview rescheduled"),
            msg("i3", T0 + timedelta(days=2), subject="Reminder: interview"),
        ]
    )
    notifier = RecordingNotifier()
    run_once(
        deps_for(
            session_factory,
            gmail,
            {
                "i1": cls("interview_invite", "Acme", "SWE", interview_datetime=first),
                "i2": cls("interview_invite", "Acme", "SWE", interview_datetime=moved),
                "i3": cls("interview_invite", "Acme", "SWE", interview_datetime=moved),
            },
            notifier=notifier,
        )
    )
    interviews = session.scalars(select(Interview)).all()
    assert len(interviews) == 1 and interviews[0].scheduled_at == moved
    assert [(a.rescheduled, a.scheduled_at) for a in notifier.alerts] == [
        (False, first),
        (True, moved),
    ]  # the identical reminder sends nothing


def test_next_round_after_past_interview_is_new(session_factory, session):
    first = T0 + timedelta(days=1)
    gmail = FakeGmail(
        messages=[
            msg("i1", T0, subject="Interview"),
            msg("i2", T0 + timedelta(days=3), subject="Final round interview"),
        ]
    )
    run_once(
        deps_for(
            session_factory,
            gmail,
            {
                "i1": cls("interview_invite", "Acme", "SWE", interview_datetime=first),
                "i2": cls("interview_invite", "Acme", "SWE"),
            },
        )
    )
    assert count(session, Interview) == 2


def test_ambiguous_match_goes_to_review(session_factory, session):
    gmail = FakeGmail(
        messages=[
            msg("c1", T0, subject="Thanks for applying"),
            msg("c2", T0 + timedelta(hours=1), subject="Thanks for applying"),
            msg("r1", T0 + timedelta(days=1), subject="Unfortunately"),
        ]
    )
    run_once(
        deps_for(
            session_factory,
            gmail,
            {
                "c1": cls("application_confirmation", "Acme", "Data Scientist"),
                "c2": cls("application_confirmation", "Acme", "Backend Engineer"),
                "r1": cls("rejection", "Acme", None),
            },
        )
    )
    assert count(session, Application) == 2  # different roles at one company are two apps
    rejection = session.scalar(select(Email).where(Email.gmail_id == "r1"))
    assert rejection.needs_review and rejection.review_reason.startswith("ambiguous_match")
    assert {a.status for a in session.scalars(select(Application))} == {"applied"}


def test_tracked_thread_reply_passes_prefilter_and_links(session_factory, session):
    gmail = FakeGmail(
        messages=[
            msg("c1", T0, subject="Thanks for applying", thread="t-acme"),
            msg(
                "rep",
                T0 + timedelta(days=1),
                subject="Re: quick question",
                sender="hm@acme.com",
                thread="t-acme",
            ),
        ]
    )
    run_once(
        deps_for(
            session_factory,
            gmail,
            {
                "c1": cls("application_confirmation", "Acme", "SWE"),
                "rep": cls("other"),
            },
        )
    )
    reply = session.scalar(select(Email).where(Email.gmail_id == "rep"))
    assert reply.application_id == apps(session)["Acme"].id


def test_api_error_keeps_cursor_and_committed_work(session_factory, session):
    gmail = FakeGmail(
        messages=[msg("ok", T0), msg("boom", T0 + timedelta(hours=1))], profile_history_id="77"
    )
    store = InMemorySyncStateStore()
    deps = deps_for(
        session_factory,
        gmail,
        {"ok": cls("application_confirmation", "Acme"), "boom": RuntimeError("API down")},
        store=store,
    )
    with pytest.raises(RuntimeError, match="API down"):
        run_once(deps)
    assert store.load() == SyncState()  # cursor not advanced
    assert [e.gmail_id for e in session.scalars(select(Email))] == ["ok"]


def test_notifier_failure_does_not_abort_run(session_factory, session):
    gmail = FakeGmail(
        messages=[msg("i1", T0, subject="Interview"), msg("c2", T0 + timedelta(hours=1))]
    )
    store = InMemorySyncStateStore()
    summary = run_once(
        deps_for(
            session_factory,
            gmail,
            {
                "i1": cls("interview_invite", "Acme", "SWE"),
                "c2": cls("application_confirmation", "Globex"),
            },
            store=store,
            notifier=RecordingNotifier(fail=True),
        )
    )
    assert summary.alerts_sent == 0 and count(session, Email) == 2
    assert store.load().last_history_id is not None


def test_reapply_after_rejection_creates_new_application(session_factory, session):
    gmail = FakeGmail(
        messages=[
            msg("c1", T0, subject="Thanks for applying"),
            msg("r1", T0 + timedelta(days=1), subject="Unfortunately"),
            msg("c2", T0 + timedelta(days=30), subject="Thanks for applying"),
        ]
    )
    run_once(
        deps_for(
            session_factory,
            gmail,
            {
                "c1": cls("application_confirmation", "Acme", "SWE"),
                "r1": cls("rejection", "Acme", "SWE"),
                "c2": cls("application_confirmation", "Acme", "SWE"),
            },
        )
    )
    statuses = sorted(a.status for a in session.scalars(select(Application)))
    assert statuses == ["applied", "rejected"]


def test_missing_config_and_scheduler_guard(tmp_path):
    s = Settings(_env_file=None, gmail_readonly_token_file=tmp_path / "nope.json")
    assert set(missing_config(s)) == {"DATABASE_URL", "ANTHROPIC_API_KEY"}
    assert start_scheduler(s) is None
