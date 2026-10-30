import json
from datetime import UTC, datetime, timedelta

import pytest

from app.gmail.auth import (
    READONLY_SCOPES,
    SEND_SCOPES,
    GmailAuthError,
    check_scopes,
    load_credentials,
)
from app.gmail.fetcher import GmailFetcher
from app.gmail.message import extract_body, html_to_text, parse_message
from app.gmail.prefilter import KEYWORD_RE, gmail_search_query, match_ats_domain, prefilter
from app.gmail.sync_state import JsonFileSyncStateStore, SyncState
from tests.fake_gmail import FakeGmail, b64, http_error, make_message

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
READ_ONLY_CALLS = {
    "users.getProfile",
    "users.history.list",
    "users.messages.list",
    "users.messages.get",
}


def fetcher_for(fake, **kw):
    kw.setdefault("pace_seconds", 0)
    return GmailFetcher(fake, now=lambda: NOW, **kw)


# --- parsing ---------------------------------------------------------------


def test_parse_message_headers_and_metadata():
    raw = make_message(
        "m1",
        thread_id="t1",
        sender='"Acme Recruiting" <Jobs@Acme.COM>',
        subject="Your application",
        snippet="We&#39;ve received it",
        internal_ms=1_761_652_800_000,
    )
    msg = parse_message(raw)
    assert (msg.gmail_id, msg.thread_id) == ("m1", "t1")
    assert msg.sender_name == "Acme Recruiting"
    assert msg.sender_email == "jobs@acme.com"
    assert msg.sender_domain == "acme.com"
    assert msg.snippet == "We've received it"
    assert msg.received_at == datetime.fromtimestamp(1_761_652_800, tz=UTC)
    assert msg.body_text is None


def test_extract_body_prefers_plain_text_in_multipart():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("Plain version")}},
            {"mimeType": "text/html", "body": {"data": b64("<p>HTML version</p>")}},
        ],
    }
    assert extract_body(payload) == "Plain version"


def test_extract_body_falls_back_to_html_and_skips_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "text/html",
                "body": {"data": b64("<style>x{}</style><p>Hi&nbsp;there</p><p>Next</p>")},
            },
            {"mimeType": "text/plain", "filename": "resume.txt", "body": {"data": b64("attach")}},
        ],
    }
    assert extract_body(payload) == "Hi\xa0there\nNext"


def test_html_to_text_drops_script():
    assert html_to_text("<script>alert(1)</script>Hello <b>world</b>") == "Hello world"


def test_body_is_not_in_repr():
    msg = parse_message(make_message("m1", body="SECRET BODY TEXT"))
    assert msg.body_text == "SECRET BODY TEXT"
    assert "SECRET BODY TEXT" not in repr(msg)


# --- prefilter -------------------------------------------------------------


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("greenhouse.io", "greenhouse.io"),
        ("us.greenhouse-mail.io", "greenhouse-mail.io"),
        ("hire.lever.co", "lever.co"),
        ("acme.myworkdayjobs.com", "myworkdayjobs.com"),
        ("myworkday.com", "myworkday.com"),
        ("ashbyhq.com", "ashbyhq.com"),
        ("smartrecruiters.com", "smartrecruiters.com"),
        ("talent.icims.com", "icims.com"),
        ("linkedin.com", None),
        ("applytojob.com", "applytojob.com"),
        ("inbound.workablemail.com", "workablemail.com"),
        ("workflow.mail.us2.cloud.oracle.com", "cloud.oracle.com"),
        ("workflow.mail.us6.oraclecloud.com", "oraclecloud.com"),
        ("oracle.com", None),
        ("rippling.com", None),
        ("notlever.co", None),
        ("lever.co.evil.com", None),
        ("gmail.com", None),
    ],
)
def test_match_ats_domain(domain, expected):
    assert match_ats_domain(domain) == expected


def _msg(**kw):
    return parse_message(make_message("m1", **kw))


def test_prefilter_passes_ats_sender_without_keywords():
    d = prefilter(_msg(sender="no-reply@hire.lever.co", subject="Hi there"))
    assert d.passed and d.reason == "ats:lever.co"


@pytest.mark.parametrize(
    "subject,snippet,keyword",
    [
        ("Thank you for your application", "", "application"),
        ("Thanks for applying to Acme", "", "applying"),
        ("Interview availability", "", "interview"),
        ("Update", "Unfortunately we have decided", "unfortunately"),
        ("Re: chat", "Here are the next steps for", "next steps"),
        ("Online assessment from Acme", "", "assessment"),
        ("Your offer letter", "", "offer"),
        ("Thank You for Your Interest in Striveworks", "", "thank you for your interest"),
        ("Lok, we've received your resume", "", "received your resume"),
        ("Update on your candidacy", "", "candidacy"),
    ],
)
def test_prefilter_keywords(subject, snippet, keyword):
    d = prefilter(_msg(subject=subject, snippet=snippet))
    assert d.passed and d.reason == f"keyword:{keyword}"


@pytest.mark.parametrize("subject", ["Weekly newsletter", "Your receipt", "Offering discounts"])
def test_prefilter_drops_unrelated(subject):
    d = prefilter(_msg(subject=subject))
    assert not d.passed and d.reason == "no_match"


@pytest.mark.parametrize("label", ["SENT", "DRAFT", "SPAM", "TRASH"])
def test_prefilter_drops_own_and_junk_mail_even_from_ats(label):
    d = prefilter(_msg(sender="x@greenhouse.io", subject="Interview", labels=(label,)))
    assert not d.passed and d.reason == f"label:{label}"


@pytest.mark.parametrize(
    "sender,subject",
    [
        ("michael@hey.simplify.jobs", "Your offer is coming—just keep going"),
        ("copilot@optimhire.ai", "Your job applications this week"),
        ("info@bb3.wayup.com", "Lok, keep applying"),
        ("jobs-noreply@linkedin.com", "Your application was sent to Acme"),
        ("inmail-hit-reply@linkedin.com", "Interview opportunity"),
    ],
)
def test_prefilter_blocks_job_board_marketing(sender, subject):
    d = prefilter(_msg(sender=sender, subject=subject))
    assert not d.passed and d.reason.startswith("blocked:")


@pytest.mark.parametrize(
    "sender,subject",
    [
        ("noreply@simplify.jobs", "Your application has been submitted"),
        ("info@wayup.com", "Thank you for your interest in the AI Engineer role"),
    ],
)
def test_prefilter_block_does_not_catch_parent_domains(sender, subject):
    assert prefilter(_msg(sender=sender, subject=subject)).passed


def test_prefilter_tracked_thread_beats_block():
    d = prefilter(_msg(sender="x@optimhire.com", thread_id="t1"), tracked_thread_ids={"t1"})
    assert d.passed


def test_prefilter_passes_tracked_thread_reply():
    d = prefilter(
        _msg(thread_id="t-out", subject="Re: quick question"), tracked_thread_ids={"t-out"}
    )
    assert d.passed and d.reason == "tracked_thread"


# --- fetcher ---------------------------------------------------------------


def test_bootstrap_uses_bounded_backfill_and_profile_history_id():
    fake = FakeGmail(messages=[make_message("a"), make_message("b")], profile_history_id="500")
    result = fetcher_for(fake, initial_backfill_days=30).fetch_new(SyncState())

    assert result.mode == "bootstrap"
    assert result.history_id == "500"
    assert [m.gmail_id for m in result.messages] == ["a", "b"]
    list_call = next(kw for name, kw in fake.calls if name == "users.messages.list")
    assert (
        list_call["q"]
        == f"after:{int((NOW - timedelta(days=30)).timestamp())} " + gmail_search_query()
    )


def test_incremental_paginates_dedupes_and_returns_latest_history_id():
    fake = FakeGmail(
        messages=[make_message(i) for i in ("a", "b", "c")],
        history_pages=[
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}]}
                ],
                "nextPageToken": "1",
                "historyId": "101",
            },
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "b"}}]},
                    {"messagesAdded": [{"message": {"id": "c"}}]},
                    {"labelsAdded": []},
                ],
                "historyId": "102",
            },
        ],
    )
    result = fetcher_for(fake).fetch_new(SyncState(last_history_id="100"))

    assert result.mode == "incremental"
    assert result.history_id == "102"
    assert [m.gmail_id for m in result.messages] == ["a", "b", "c"]
    history_calls = [kw for name, kw in fake.calls if name == "users.history.list"]
    assert all(kw["startHistoryId"] == "100" for kw in history_calls)
    assert all(kw["historyTypes"] == ["messageAdded"] for kw in history_calls)
    assert not any(name == "users.messages.list" for name, _ in fake.calls)  # no rescan


def test_incremental_with_no_changes():
    fake = FakeGmail(history_pages=[{"historyId": "100"}])
    result = fetcher_for(fake).fetch_new(SyncState(last_history_id="100"))
    assert result.messages == [] and result.history_id == "100"


def test_expired_history_catches_up_from_last_run():
    last_run = datetime(2026, 10, 1, tzinfo=UTC)
    fake = FakeGmail(
        messages=[make_message("x")], history_pages=[http_error(404)], profile_history_id="777"
    )
    result = fetcher_for(fake).fetch_new(SyncState(last_history_id="1", last_run_at=last_run))

    assert result.mode == "catchup"
    assert result.history_id == "777"
    assert [m.gmail_id for m in result.messages] == ["x"]
    list_call = next(kw for name, kw in fake.calls if name == "users.messages.list")
    assert list_call["q"] == f"after:{int(last_run.timestamp())} " + gmail_search_query()


def test_other_history_errors_propagate():
    fake = FakeGmail(history_pages=[http_error(500)])
    with pytest.raises(Exception, match="500"):
        fetcher_for(fake).fetch_new(SyncState(last_history_id="1"))


def test_message_deleted_before_get_is_skipped():
    fake = FakeGmail(
        messages=[make_message("a")],
        history_pages=[
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "gone"}}, {"message": {"id": "a"}}]}
                ],
                "historyId": "2",
            }
        ],
    )
    result = fetcher_for(fake).fetch_new(SyncState(last_history_id="1"))
    assert [m.gmail_id for m in result.messages] == ["a"]


def test_metadata_fetch_requests_headers_only_and_full_fetch_gets_body():
    fake = FakeGmail(messages=[make_message("a", body="Body")])
    fetcher = fetcher_for(fake)
    fetcher.get_metadata("a")
    full = fetcher.get_full("a")
    gets = [kw for name, kw in fake.calls if name == "users.messages.get"]
    assert gets[0]["format"] == "metadata"
    assert gets[1]["format"] == "full"
    assert full.body_text == "Body"


def test_fetcher_only_makes_read_calls():
    fake = FakeGmail(
        messages=[make_message("a")],
        history_pages=[
            {"history": [{"messagesAdded": [{"message": {"id": "a"}}]}], "historyId": "2"}
        ],
    )
    fetcher = fetcher_for(fake)
    fetcher.fetch_new(SyncState())
    fetcher.fetch_new(SyncState(last_history_id="1"))
    fetcher.preview(7)
    fetcher.get_full("a")
    assert {name for name, _ in fake.calls} <= READ_ONLY_CALLS


# --- sync state ------------------------------------------------------------


def test_json_sync_state_round_trip(tmp_path):
    store = JsonFileSyncStateStore(tmp_path / "state.json")
    assert store.load() == SyncState()
    state = SyncState(last_history_id="42", last_run_at=NOW)
    store.save(state)
    assert store.load() == state


# --- auth ------------------------------------------------------------------


def test_check_scopes_exact_match():
    check_scopes(list(READONLY_SCOPES), READONLY_SCOPES)


@pytest.mark.parametrize(
    "granted",
    [None, [], list(SEND_SCOPES), list(READONLY_SCOPES + SEND_SCOPES)],
)
def test_check_scopes_rejects_missing_or_extra(granted):
    with pytest.raises(GmailAuthError):
        check_scopes(granted, READONLY_SCOPES)


def test_load_credentials_missing_token_explains_fix(tmp_path):
    with pytest.raises(GmailAuthError, match="scripts.gmail_auth readonly"):
        load_credentials(tmp_path / "token.json", READONLY_SCOPES)


def test_load_credentials_refuses_tracker_token_with_send_scope(tmp_path):
    token = tmp_path / "token.json"
    token.write_text(
        json.dumps(
            {
                "token": "t",
                "refresh_token": "r",
                "client_id": "c",
                "client_secret": "s",
                "scopes": list(READONLY_SCOPES + SEND_SCOPES),
            }
        )
    )
    with pytest.raises(GmailAuthError, match="do not match"):
        load_credentials(token, READONLY_SCOPES)


def test_gmail_search_covers_local_rules():
    q = gmail_search_query()
    assert q.startswith("{") and q.endswith("-from:tsenta.com")
    assert "-from:linkedin.com" in q and "from:linkedin.com " not in q.split("}")[0] + " "
    assert "from:greenhouse-mail.io" in q and "from:lever.co" in q
    from app.gmail.prefilter import SEARCH_KEYWORDS

    for term in SEARCH_KEYWORDS:
        assert KEYWORD_RE.search(term.strip('"')), term  # every search term is a local keyword


def test_preview_is_not_narrowed():
    fake = FakeGmail(messages=[make_message("a")])
    fetcher_for(fake).preview(7)
    q = next(kw for name, kw in fake.calls if name == "users.messages.list")["q"]
    assert q == f"after:{int((NOW - timedelta(days=7)).timestamp())}"


def test_calls_are_paced():
    sleeps = []
    fake = FakeGmail(messages=[make_message(i) for i in ("a", "b", "c")])
    fetcher = GmailFetcher(fake, now=lambda: NOW, pace_seconds=10, sleep=sleeps.append)
    fetcher.fetch_new(SyncState())
    assert len(sleeps) >= 4 and all(0 < s <= 10 for s in sleeps)
