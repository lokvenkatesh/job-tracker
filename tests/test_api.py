from datetime import UTC, datetime, timedelta

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.models import Application, Email, Interview, StatusEvent, utcnow
from app.main import create_app

PASSWORD = "correct horse battery"
HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


def settings(**kw):
    base = dict(
        _env_file=None,
        dashboard_username="me",
        dashboard_password_hash=HASH,
        session_secret="test-secret",
        scheduler_enabled=False,
        timezone="America/New_York",
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(settings(), session_factory))


@pytest.fixture
def authed(client):
    r = client.post("/auth/login", data={"username": "me", "password": PASSWORD})
    assert r.status_code == 200
    return client


def add_app(s, company, role=None, status="applied", *, at=T0, needs_review=False):
    a = Application(
        company=company,
        role=role,
        status=status,
        applied_date=at.date(),
        last_update=at,
        status_changed_at=at,
        source="test",
        needs_review=needs_review,
    )
    s.add(a)
    s.flush()
    return a


def add_email(
    s, gmail_id, type_, *, app=None, at=T0, needs_review=False, company=None, reason=None
):
    e = Email(
        gmail_id=gmail_id,
        thread_id=f"t-{gmail_id}",
        application_id=app.id if app else None,
        type=type_,
        received_at=at,
        confidence=0.9,
        sender_email="x@example.com",
        subject=f"subject {gmail_id}",
        company=company,
        needs_review=needs_review,
        review_reason=reason,
    )
    s.add(e)
    s.flush()
    return e


# --- auth ------------------------------------------------------------------


def test_healthz_is_public(client):
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/applications"),
        ("get", "/applications/1"),
        ("patch", "/applications/1"),
        ("get", "/stats"),
        ("get", "/review-queue"),
        ("post", "/review-queue/emails/1"),
        ("get", "/auth/me"),
    ],
)
def test_endpoints_require_login(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_login_logout(client):
    assert (
        client.post("/auth/login", data={"username": "me", "password": "nope"}).status_code == 401
    )
    assert (
        client.post("/auth/login", data={"username": "you", "password": PASSWORD}).status_code
        == 401
    )
    assert (
        client.post("/auth/login", data={"username": "me", "password": PASSWORD}).status_code == 200
    )
    assert client.get("/auth/me").json() == {"user": "me"}
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401


def test_session_cookie_is_signed(client):
    client.cookies.set("jt_session", "forged")
    assert client.get("/auth/me").status_code == 401


def test_lockout_after_repeated_failures(client):
    for _ in range(5):
        client.post("/auth/login", data={"username": "me", "password": "wrong"})
    r = client.post("/auth/login", data={"username": "me", "password": PASSWORD})
    assert r.status_code == 429


def test_login_unconfigured(session_factory):
    c = TestClient(create_app(settings(dashboard_password_hash=None), session_factory))
    r = c.post("/auth/login", data={"username": "me", "password": PASSWORD})
    assert r.status_code == 503 and "hash_password" in r.json()["detail"]


def test_password_hash_survives_env_file_quoting(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"DASHBOARD_PASSWORD_HASH='{HASH}'\n")
    assert Settings(_env_file=env).dashboard_password_hash.get_secret_value() == HASH


def test_https_only_cookie_in_production(session_factory):
    c = TestClient(
        create_app(settings(app_env="production"), session_factory), base_url="https://t"
    )
    r = c.post("/auth/login", data={"username": "me", "password": PASSWORD})
    assert "secure" in r.headers["set-cookie"].lower()
    assert "samesite=lax" in r.headers["set-cookie"].lower()


# --- applications ----------------------------------------------------------


def test_list_filter_search_sort(authed, session_factory):
    with session_factory() as s:
        add_app(s, "Acme", "ML Engineer", at=T0)
        add_app(s, "Globex", "Data Scientist", "rejected", at=T0 + timedelta(days=1))
        add_app(s, "Initech", "ML Platform", "interview", at=T0 + timedelta(days=2))
        s.commit()

    r = authed.get("/applications").json()
    assert r["total"] == 3 and [a["company"] for a in r["items"]] == ["Initech", "Globex", "Acme"]
    r = authed.get("/applications", params={"status": ["interview", "rejected"]}).json()
    assert {a["company"] for a in r["items"]} == {"Initech", "Globex"}
    r = authed.get("/applications", params={"q": "ml"}).json()
    assert {a["company"] for a in r["items"]} == {"Acme", "Initech"}
    r = authed.get("/applications", params={"sort": "company", "limit": 2}).json()
    assert r["total"] == 3 and [a["company"] for a in r["items"]] == ["Acme", "Globex"]
    assert authed.get("/applications", params={"sort": "id; drop"}).status_code == 422


def test_application_detail(authed, session_factory):
    with session_factory() as s:
        a = add_app(s, "Acme", "SWE")
        e = add_email(s, "g1", "application_confirmation", app=a)
        s.add(StatusEvent(application_id=a.id, new_status="applied", email_id=e.id))
        s.add(
            Interview(
                application_id=a.id,
                scheduled_at=T0 + timedelta(days=3),
                created_at=T0,
                updated_at=T0,
            )
        )
        s.commit()
        app_id = a.id

    d = authed.get(f"/applications/{app_id}").json()
    assert d["company"] == "Acme"
    assert [ev["new_status"] for ev in d["events"]] == ["applied"]
    assert d["emails"][0]["gmail_url"].endswith("#all/g1")
    assert len(d["interviews"]) == 1
    assert authed.get("/applications/9999").status_code == 404


def test_patch_application(authed, session_factory):
    with session_factory() as s:
        a = add_app(s, "Acme", None, "interview", needs_review=True)
        s.commit()
        app_id = a.id

    r = authed.patch(
        f"/applications/{app_id}",
        json={
            "role": "SWE",
            "status": "applied",
            "needs_review": False,
            "applied_date": "2026-09-30",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert (body["role"], body["status"], body["needs_review"], body["applied_date"]) == (
        "SWE",
        "applied",
        False,
        "2026-09-30",
    )  # manual edits may move status backward
    events = authed.get(f"/applications/{app_id}").json()["events"]
    assert [(e["old_status"], e["new_status"], e["email_id"]) for e in events] == [
        ("interview", "applied", None)
    ]
    assert authed.patch(f"/applications/{app_id}", json={"company": None}).status_code == 422
    assert authed.patch(f"/applications/{app_id}", json={"bogus": 1}).status_code == 422
    assert authed.patch(f"/applications/{app_id}", json={"status": "ghosted"}).status_code == 422


# --- review queue ----------------------------------------------------------


def test_review_queue_and_resolutions(authed, session_factory):
    with session_factory() as s:
        acme = add_app(s, "Acme", "SWE", at=T0)
        flagged = add_app(s, "Flagged", needs_review=True)
        dismiss = add_email(s, "d1", "other", needs_review=True, reason="low_confidence")
        link = add_email(
            s,
            "l1",
            "rejection",
            at=T0 + timedelta(days=2),
            needs_review=True,
            reason="ambiguous_match:multiple_candidates",
        )
        create = add_email(
            s,
            "c1",
            "application_confirmation",
            needs_review=True,
            company="Globex",
            reason="ambiguous_match:x",
        )
        nocompany = add_email(
            s, "n1", "application_confirmation", needs_review=True, reason="no_company"
        )
        done = add_email(s, "ok", "rejection")
        s.commit()
        ids = dict(
            acme=acme.id,
            flagged=flagged.id,
            dismiss=dismiss.id,
            link=link.id,
            create=create.id,
            nocompany=nocompany.id,
            done=done.id,
        )

    q = authed.get("/review-queue").json()
    assert {e["gmail_id"] for e in q["emails"]} == {"d1", "l1", "c1", "n1"}
    assert [a["id"] for a in q["applications"]] == [ids["flagged"]]

    assert (
        authed.post(f"/review-queue/emails/{ids['dismiss']}", json={"action": "dismiss"}).json()[
            "needs_review"
        ]
        is False
    )

    r = authed.post(f"/review-queue/emails/{ids['link']}", json={"action": "link"})
    assert r.status_code == 422
    r = authed.post(
        f"/review-queue/emails/{ids['link']}",
        json={"action": "link", "application_id": ids["acme"]},
    )
    assert r.json()["application_id"] == ids["acme"]
    assert authed.get(f"/applications/{ids['acme']}").json()["status"] == "rejected"

    r = authed.post(
        f"/review-queue/emails/{ids['create']}", json={"action": "create", "role": "Analyst"}
    )
    new_app = authed.get(f"/applications/{r.json()['application_id']}").json()
    assert (new_app["company"], new_app["role"], new_app["status"], new_app["source"]) == (
        "Globex",
        "Analyst",
        "applied",
        "review",
    )
    assert new_app["applied_date"] == "2026-10-01"

    r = authed.post(f"/review-queue/emails/{ids['nocompany']}", json={"action": "create"})
    assert r.status_code == 422
    r = authed.post(
        f"/review-queue/emails/{ids['nocompany']}", json={"action": "create", "company": "Hooli"}
    )
    assert r.status_code == 200

    assert (
        authed.post(f"/review-queue/emails/{ids['done']}", json={"action": "dismiss"}).status_code
        == 409
    )
    assert authed.post("/review-queue/emails/9999", json={"action": "dismiss"}).status_code == 404
    assert authed.get("/review-queue").json()["emails"] == []


def test_link_with_explicit_status_overrides_state_machine(authed, session_factory):
    with session_factory() as s:
        a = add_app(s, "Acme", status="interview", at=T0 + timedelta(days=5))
        e = add_email(s, "old", "assessment", at=T0, needs_review=True)
        s.commit()
        app_id, email_id = a.id, e.id
    authed.post(
        f"/review-queue/emails/{email_id}", json={"action": "link", "application_id": app_id}
    )
    assert authed.get(f"/applications/{app_id}").json()["status"] == "interview"  # older email


# --- stats -----------------------------------------------------------------


def test_stats(authed, session_factory):
    now = utcnow()
    with session_factory() as s:
        a1 = add_app(s, "A", status="applied", at=now)
        a2 = add_app(s, "B", status="rejected", at=now)
        a3 = add_app(s, "C", status="rejected", at=now)
        a4 = add_app(s, "D", status="offer", at=now - timedelta(weeks=3))
        for app, statuses in [
            (a1, ["applied"]),
            (a2, ["applied", "rejected"]),
            (a3, ["applied", "assessment", "interview", "rejected"]),
            (a4, ["applied", "interview", "offer"]),
        ]:
            prev = None
            for st in statuses:
                s.add(StatusEvent(application_id=app.id, old_status=prev, new_status=st))
                prev = st
        s.add(
            Interview(
                application_id=a4.id,
                scheduled_at=now + timedelta(days=1),
                meeting_link="https://zoom.us/j/1",
                created_at=now,
                updated_at=now,
            )
        )
        s.add(
            Interview(
                application_id=a3.id,
                scheduled_at=now + timedelta(days=2),
                created_at=now,
                updated_at=now,
            )
        )  # rejected app
        s.add(
            Interview(
                application_id=a4.id,
                scheduled_at=now - timedelta(days=9),
                created_at=now,
                updated_at=now,
            )
        )  # past
        add_email(s, "r1", "recruiter_outreach")
        add_email(s, "q1", "other", needs_review=True)
        s.commit()

    st = authed.get("/stats").json()
    assert st["total_applications"] == 4
    assert st["by_status"] == {
        "applied": 1,
        "assessment": 0,
        "interview": 0,
        "offer": 1,
        "rejected": 2,
    }
    assert [f["reached"] for f in st["funnel"]] == [4, 2, 2, 1]  # cumulative: D skipped assessment
    assert st["response_rate"] == 0.75
    assert st["interview_rate"] == 0.5
    assert st["needs_review"] == 1 and st["recruiter_outreach"] == 1
    assert len(st["weekly"]) == 12 and sum(w["applications"] for w in st["weekly"]) == 4
    assert [u["company"] for u in st["upcoming_interviews"]] == ["D"]


def test_stats_empty(authed):
    st = authed.get("/stats").json()
    assert st["total_applications"] == 0 and st["response_rate"] == 0.0


def test_db_not_configured():
    c = TestClient(create_app(settings(database_url=None), None))
    c.post("/auth/login", data={"username": "me", "password": PASSWORD})
    assert c.get("/applications").status_code == 503
