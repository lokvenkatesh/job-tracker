import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.config import PROJECT_ROOT, Settings
from app.db.models import Email, MailAccount, OutreachCampaign, OutreachEmail, SyncStateRow
from app.gmail import accounts
from app.gmail.accounts import (
    AccountError,
    OAuthResult,
    check_token_scopes,
    credentials_for,
    decrypt,
    encrypt,
    save_connection,
)
from app.main import create_app
from app.outreach.replies import record_reply
from app.outreach.sender import send_next
from tests.test_api import PASSWORD
from tests.test_api import settings as api_settings
from tests.test_sending import FakeSend, campaign, inbound, outreach

KEY = Fernet.generate_key().decode()
READ, SEND = accounts.GMAIL_SCOPE["read"], accounts.GMAIL_SCOPE["send"]
OPENID, EMAIL_SCOPE = accounts.IDENTITY_SCOPES
NOW = datetime(2026, 11, 2, 15, tzinfo=UTC)


def cfg(**kw):
    return Settings(**{"_env_file": None, "token_encryption_key": KEY, **kw})


def token_json(scopes, *, expired=False, refresh=True):
    return Credentials(
        token="access",
        refresh_token="refresh" if refresh else None,
        client_id="cid",
        client_secret="secret",
        token_uri="https://oauth2.googleapis.com/token",
        scopes=scopes,
        expiry=(datetime.now(UTC) + timedelta(hours=-1 if expired else 1)).replace(tzinfo=None),
    ).to_json()


# --- encryption & scopes ------------------------------------------------------------


def test_encryption_round_trip_and_wrong_key():
    s = cfg()
    secret = encrypt(s, "token-data")
    assert secret != "token-data" and decrypt(s, secret) == "token-data"
    with pytest.raises(AccountError, match="can't be decrypted"):
        decrypt(cfg(token_encryption_key=Fernet.generate_key().decode()), secret)
    with pytest.raises(AccountError, match="not set"):
        encrypt(Settings(_env_file=None), "x")


@pytest.mark.parametrize(
    "granted,kind,ok",
    [
        ([READ, OPENID, EMAIL_SCOPE], "read", True),
        ([READ], "read", True),  # tokens imported from token.json
        ([SEND, OPENID, EMAIL_SCOPE], "send", True),
        ([READ, SEND], "read", False),
        ([SEND, READ], "send", False),
        ([OPENID, EMAIL_SCOPE], "read", False),
        ([READ, "https://www.googleapis.com/auth/drive"], "read", False),
    ],
)
def test_token_scope_rules(granted, kind, ok):
    if ok:
        check_token_scopes(granted, kind)
    else:
        with pytest.raises(AccountError):
            check_token_scopes(granted, kind)


# --- saving connections ------------------------------------------------------------------


def result(email="me@gmail.com", scopes=(READ, OPENID, EMAIL_SCOPE)):
    return OAuthResult(email=email, token_json=token_json(list(scopes)))


def test_connect_read_then_send(session):
    s = cfg()
    a = save_connection(session, s, result(), "read", label=None, backfill_days=14)
    assert (a.email, a.label, a.initial_backfill_days, a.enabled) == (
        "me@gmail.com",
        "me",
        14,
        True,
    )
    assert "refresh" not in a.readonly_token  # stored encrypted
    assert json.loads(decrypt(s, a.readonly_token))["refresh_token"] == "refresh"
    save_connection(session, s, result(scopes=(SEND, OPENID, EMAIL_SCOPE)), "send", account_id=a.id)
    assert a.send_token and a.readonly_token


def test_send_needs_existing_matching_inbox(session):
    s = cfg()
    with pytest.raises(AccountError, match="reading first"):
        save_connection(session, s, result(), "send")
    a = save_connection(session, s, result(), "read")
    with pytest.raises(AccountError, match="signed in as other@gmail.com"):
        save_connection(session, s, result("other@gmail.com"), "send", account_id=a.id)


def test_reconnect_updates_same_inbox(session):
    s = cfg()
    a = save_connection(session, s, result(), "read", label="Primary")
    a.last_error = "needs reconnecting"
    old = a.readonly_token
    b = save_connection(session, s, result(), "read", label="ignored")
    assert (
        b.id == a.id and b.label == "Primary" and b.readonly_token != old and b.last_error is None
    )
    assert session.scalar(select(MailAccount.id).where(MailAccount.id != a.id)) is None


def test_disconnect_keeps_data(session):
    s = cfg()
    a = save_connection(session, s, result(), "read")
    session.add(
        Email(
            gmail_id="g",
            thread_id="t",
            mail_account_id=a.id,
            type="other",
            received_at=NOW,
            confidence=1,
            sender_email="x@y.z",
            subject="s",
        )
    )
    accounts.disconnect(session, a, "all")
    assert a.readonly_token is None and a.send_token is None and not a.enabled
    assert session.scalar(select(Email.mail_account_id)) == a.id


# --- credentials per inbox ---------------------------------------------------------------


def test_credentials_valid_and_expired(session_factory, monkeypatch):
    s = cfg()
    with session_factory() as db:
        a = save_connection(db, s, OAuthResult("me@gmail.com", token_json([READ])), "read")
        db.commit()
        account_id = a.id
    with session_factory() as db:
        creds = credentials_for(db.get(MailAccount, account_id), "read", s, session_factory)
        assert creds.token == "access"
        with pytest.raises(AccountError, match="no send permission"):
            credentials_for(db.get(MailAccount, account_id), "send", s, session_factory)

    def fake_refresh(self, request):
        self.token = "new-access"
        self.expiry = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    with session_factory() as db:
        db.get(MailAccount, account_id).readonly_token = encrypt(
            s, token_json([READ], expired=True)
        )
        db.commit()
    with session_factory() as db:
        assert (
            credentials_for(db.get(MailAccount, account_id), "read", s, session_factory).token
            == "new-access"
        )
    with session_factory() as db:  # refreshed token was saved, still encrypted
        stored = db.get(MailAccount, account_id).readonly_token
        assert json.loads(decrypt(s, stored))["token"] == "new-access"


def test_expired_without_refresh_token_needs_reconnect(session):
    s = cfg()
    a = save_connection(
        session,
        s,
        OAuthResult("me@gmail.com", token_json([READ], expired=True, refresh=False)),
        "read",
    )
    with pytest.raises(AccountError, match="needs reconnecting"):
        credentials_for(a, "read", s, None)


# --- one-time import of token files --------------------------------------------------


def test_import_legacy_tokens(session_factory, tmp_path, monkeypatch):
    (tmp_path / "token.json").write_text(token_json([READ]))
    (tmp_path / "token_send.json").write_text(token_json([SEND]))

    class Profile:
        def users(self):
            return self

        def getProfile(self, userId):
            return self

        def execute(self):
            return {"emailAddress": "Me@Gmail.com"}

    monkeypatch.setattr(accounts, "build_gmail_service", lambda creds: Profile())
    with session_factory() as db:
        db.add(SyncStateRow(id=1, last_history_id="25292", last_run_at=NOW))
        c = OutreachCampaign(jd_text="jd")
        db.add(c)
        db.flush()
        db.add(
            OutreachEmail(
                campaign_id=c.id, recipient_email="a@b.c", subject="s", body="b", status="sent"
            )
        )
        db.add(
            Email(
                gmail_id="g1",
                thread_id="t",
                type="other",
                received_at=NOW,
                confidence=1,
                sender_email="x@y.z",
                subject="s",
            )
        )
        db.commit()
    s = cfg(
        gmail_readonly_token_file=tmp_path / "token.json",
        gmail_send_token_file=tmp_path / "token_send.json",
    )
    accounts.import_legacy_tokens(s, session_factory)
    accounts.import_legacy_tokens(s, session_factory)  # idempotent
    with session_factory() as db:
        (a,) = db.scalars(select(MailAccount)).all()
        assert (a.email, a.label, a.last_history_id) == ("me@gmail.com", "Primary", "25292")
        assert a.readonly_token and a.send_token
        assert db.scalar(select(Email.mail_account_id)) == a.id
        assert db.scalar(select(OutreachEmail.mail_account_id)) == a.id
        assert db.scalar(select(OutreachCampaign.mail_account_id)) == a.id


def test_import_skips_without_token_file(session_factory, tmp_path):
    s = cfg(gmail_readonly_token_file=tmp_path / "missing.json")
    assert accounts.import_legacy_tokens(s, session_factory) is s


# --- scanning several inboxes -----------------------------------------------------------


def test_tracker_scans_each_inbox_independently(tmp_path, monkeypatch):
    from app.pipeline import scheduler
    from tests.fake_gmail import FakeGmail
    from tests.test_pipeline import RecordingNotifier, ScriptedClassifier, cls, msg

    db_url = f"sqlite:///{tmp_path / 'multi.db'}"
    engine = create_engine(db_url)
    with engine.begin() as conn:
        c = Config(str(PROJECT_ROOT / "alembic.ini"))
        c.attributes["connection"] = conn
        command.upgrade(c, "head")
    factory = sessionmaker(bind=engine)
    s = cfg(database_url=db_url, embeddings_enabled=False, timezone="America/New_York")
    with factory() as db:
        for email, enabled in [
            ("one@gmail.com", True),
            ("broken@gmail.com", True),
            ("paused@gmail.com", False),
        ]:
            db.add(
                MailAccount(
                    email=email,
                    label=email,
                    readonly_token="x",
                    enabled=enabled,
                    initial_backfill_days=30,
                )
            )
        db.commit()

    gmails = {
        "one@gmail.com": FakeGmail(
            messages=[msg("a1", NOW, subject="Thanks for applying")], profile_history_id="100"
        ),
    }

    def fake_service(account, kind, settings, factory_):
        if account.email == "broken@gmail.com":
            raise AccountError("broken@gmail.com needs reconnecting")
        return gmails[account.email]

    monkeypatch.setattr(scheduler, "gmail_service", fake_service)
    monkeypatch.setattr(
        scheduler,
        "EmailClassifier",
        lambda settings: ScriptedClassifier({"a1": cls("application_confirmation", "Acme")}),
    )
    monkeypatch.setattr(scheduler, "build_notifier", lambda settings: RecordingNotifier())

    results = dict(scheduler.run_tracker_job(s))
    assert set(results) == {"one@gmail.com", "broken@gmail.com"}  # paused inbox skipped
    assert results["one@gmail.com"].queued == 1
    assert results["broken@gmail.com"].startswith("failed")
    with factory() as db:
        one, broken, _ = db.scalars(select(MailAccount).order_by(MailAccount.id)).all()
        assert (one.last_history_id, one.last_error) == ("100", None)
        assert (
            broken.last_error == "broken@gmail.com needs reconnecting"
            and broken.last_history_id is None
        )
        assert db.scalar(select(Email.mail_account_id)) == one.id
    assert dict(scheduler.run_tracker_job(s, account_ids=[999])) == {}
    engine.dispose()


# --- sending from the right inbox --------------------------------------------------------


def test_sender_uses_campaign_inbox_and_followup_uses_original(session):
    personal = MailAccount(email="me@gmail.com", label="Primary", send_token="x", enabled=True)
    school = MailAccount(email="me@northeastern.edu", label="NEU", send_token="x", enabled=True)
    session.add_all([personal, school])
    session.flush()
    c = campaign(session)
    c.mail_account_id = school.id
    first = outreach(
        session,
        c,
        "p@x.com",
        status="sent",
        sent_at=NOW - timedelta(days=8),
        thread_id="t1",
        mail_account_id=personal.id,
    )
    outreach(session, c, "p@x.com", followup_of_id=first.id)
    new = outreach(session, c, "q@x.com")
    used = []

    def services_for(account):
        used.append(account.email)
        return FakeSend(), FakeSend()

    from tests.test_sending import cfg as send_cfg

    s = send_cfg(outreach_min_gap_seconds=0, outreach_max_gap_seconds=0)
    send_next(session, services_for=services_for, settings=s, now=NOW)
    send_next(session, services_for=services_for, settings=s, now=NOW + timedelta(minutes=1))
    assert used == ["me@gmail.com", "me@northeastern.edu"]  # follow-up from original's inbox
    assert new.mail_account_id == school.id


def test_sender_blocked_when_inbox_cannot_send(session):
    c = campaign(session)
    e = outreach(session, c, "p@x.com")

    def services_for(account):
        raise AccountError("No inbox is allowed to send")

    from tests.test_sending import cfg as send_cfg

    r = send_next(session, services_for=services_for, settings=send_cfg(), now=NOW)
    assert r.action == "blocked" and e.status == "approved" and "No inbox" in e.send_error


def test_replies_matched_within_the_same_inbox(session):
    a1 = MailAccount(email="me@gmail.com", label="A", enabled=True)
    a2 = MailAccount(email="me@neu.edu", label="B", enabled=True)
    session.add_all([a1, a2])
    session.flush()
    c = campaign(session)
    e = outreach(
        session, c, "p@x.com", status="sent", sent_at=NOW, thread_id="t1", mail_account_id=a1.id
    )
    assert record_reply(session, inbound("p@x.com"), mail_account_id=a2.id) is None
    assert e.status == "sent"
    assert record_reply(session, inbound("p@x.com"), mail_account_id=a1.id) is not None


# --- Inboxes page & OAuth routes ------------------------------------------------------------


@pytest.fixture
def client(session_factory, tmp_path, monkeypatch):
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "cid.apps.googleusercontent.com",
                    "client_secret": "secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    s = api_settings(token_encryption_key=KEY, gmail_credentials_file=creds)
    app = create_app(s, session_factory)
    scans = []
    from app.web import inbox_pages

    monkeypatch.setattr(
        inbox_pages, "_scan_in_background", lambda bg, account_id: scans.append(account_id)
    )
    c = TestClient(app, follow_redirects=False)
    c.post("/auth/login", data={"username": "me", "password": PASSWORD})
    c.scans = scans
    return c


def test_inboxes_page_lists_details(client, session_factory):
    with session_factory() as db:
        a = MailAccount(
            email="me@gmail.com",
            label="Primary",
            readonly_token="x",
            send_token="y",
            enabled=True,
            last_run_at=NOW,
            initial_backfill_days=30,
        )
        db.add(a)
        db.flush()
        db.add(
            Email(
                gmail_id="g",
                thread_id="t",
                mail_account_id=a.id,
                type="other",
                received_at=NOW,
                confidence=1,
                sender_email="x@y.z",
                subject="s",
            )
        )
        db.commit()
    html = client.get("/dashboard/inboxes").text
    assert "Primary" in html and "me@gmail.com" in html and "active" in html
    assert "Connect with Google" in html and "/oauth/google/callback" in html
    assert "Turn off sending" in html and "Scan now" in html


def test_connect_redirects_to_google_with_pkce(client):
    r = client.post(
        "/dashboard/inboxes/connect",
        data={"kind": "read", "label": "Northeastern", "backfill_days": "14"},
    )
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    q = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert q["redirect_uri"] == ["http://localhost:8000/oauth/google/callback"]
    assert set(q["scope"][0].split()) == {READ, OPENID, EMAIL_SCOPE}
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    assert "state" in q


def test_callback_rejects_bad_state(client):
    client.post("/dashboard/inboxes/connect", data={"kind": "read"})
    r = client.get("/oauth/google/callback", params={"state": "forged", "code": "c"})
    assert "expired" in r.headers["location"] or "error=" in r.headers["location"]


def test_callback_cancelled(client):
    client.post("/dashboard/inboxes/connect", data={"kind": "read"})
    r = client.get("/oauth/google/callback", params={"error": "access_denied"})
    assert "cancelled" in r.headers["location"]


def test_callback_success_creates_inbox_and_scans(client, session_factory, monkeypatch):
    r = client.post(
        "/dashboard/inboxes/connect", data={"kind": "read", "label": "NEU", "backfill_days": "14"}
    )
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    seen = {}

    def fake_finish(settings, kind, *, state, code_verifier, authorization_response):
        seen.update(kind=kind, verifier=code_verifier, url=authorization_response)
        return OAuthResult("me@northeastern.edu", token_json([READ, OPENID, EMAIL_SCOPE]))

    monkeypatch.setattr(accounts, "finish_oauth", fake_finish)
    r = client.get("/oauth/google/callback", params={"state": state, "code": "abc"})
    assert r.status_code == 303 and "connected=me%40northeastern.edu" in r.headers["location"]
    assert (
        seen["kind"] == "read"
        and seen["verifier"]
        and seen["url"].startswith("http://localhost:8000/oauth/google/callback?")
    )
    with session_factory() as db:
        a = db.scalars(select(MailAccount)).one()
        assert (a.label, a.initial_backfill_days, a.enabled) == ("NEU", 14, True)
    assert client.scans == [a.id]
    # the state is single-use
    r = client.get("/oauth/google/callback", params={"state": state, "code": "abc"})
    assert "error=" in r.headers["location"]


def test_inbox_actions(client, session_factory):
    with session_factory() as db:
        a = MailAccount(
            email="me@gmail.com", label="Primary", readonly_token="x", send_token="y", enabled=True
        )
        db.add(a)
        db.commit()
        aid = a.id
    client.post(f"/dashboard/inboxes/{aid}/rename", data={"label": "Personal"})
    client.post(f"/dashboard/inboxes/{aid}/pause")
    with session_factory() as db:
        a = db.get(MailAccount, aid)
        assert (a.label, a.enabled) == ("Personal", False)
    assert "paused" in client.post(f"/dashboard/inboxes/{aid}/sync").headers["location"]
    client.post(f"/dashboard/inboxes/{aid}/resume")
    client.post(f"/dashboard/inboxes/{aid}/sync")
    assert client.scans == [aid]
    client.post(f"/dashboard/inboxes/{aid}/disconnect", data={"what": "send"})
    with session_factory() as db:
        a = db.get(MailAccount, aid)
        assert a.send_token is None and a.readonly_token == "x"
    client.post(f"/dashboard/inboxes/{aid}/disconnect", data={"what": "all"})
    with session_factory() as db:
        assert db.get(MailAccount, aid).readonly_token is None
    assert client.post("/dashboard/inboxes/999/pause").status_code == 404


def test_campaign_form_send_from_choices(client, session_factory):
    with session_factory() as db:
        db.add_all(
            [
                MailAccount(email="me@gmail.com", label="Primary", send_token="y", enabled=True),
                MailAccount(email="me@neu.edu", label="NEU", send_token="y", enabled=True),
            ]
        )
        db.commit()
    html = client.get("/dashboard/outreach").text
    assert "Send from" in html and "NEU · me@neu.edu" in html


def test_inboxes_require_login(session_factory):
    c = TestClient(create_app(api_settings(), session_factory), follow_redirects=False)
    assert c.get("/dashboard/inboxes").status_code == 303
    assert c.get("/oauth/google/callback").status_code == 303
