from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db.models import Application, Email, Interview, StatusEvent, utcnow
from app.main import create_app
from app.web.charts import nice_max, weekly_bars
from tests.test_api import PASSWORD, settings

T0 = datetime(2026, 10, 20, 15, tzinfo=UTC)
HX = {"HX-Request": "true"}


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(settings(), session_factory), follow_redirects=False)


@pytest.fixture
def authed(client):
    r = client.post("/login", data={"username": "me", "password": PASSWORD, "next": "/dashboard"})
    assert r.status_code == 303
    return client


@pytest.fixture
def seeded(session_factory):
    with session_factory() as s:
        acme = Application(
            company="Acme",
            role="ML Engineer",
            status="interview",
            applied_date=date(2026, 10, 1),
            last_update=T0,
            status_changed_at=T0,
            source="email",
            needs_review=False,
        )
        evil = Application(
            company="<script>alert(1)</script>",
            role=None,
            status="applied",
            last_update=T0,
            status_changed_at=T0,
            source="email",
            needs_review=True,
        )
        s.add_all([acme, evil])
        s.flush()
        conf = Email(
            gmail_id="g-conf",
            thread_id="t1",
            application_id=acme.id,
            type="application_confirmation",
            received_at=T0 - timedelta(days=5),
            confidence=0.95,
            sender_email="no-reply@ashbyhq.com",
            subject="Thanks for applying to Acme",
        )
        flagged = Email(
            gmail_id="g-flag",
            thread_id="t2",
            type="rejection",
            received_at=T0,
            confidence=0.6,
            sender_email="x@y.com",
            subject="An update on your application",
            company="Acme",
            needs_review=True,
            review_reason="low_confidence",
        )
        s.add_all([conf, flagged])
        s.flush()
        s.add(StatusEvent(application_id=acme.id, new_status="applied", email_id=conf.id))
        s.add(StatusEvent(application_id=acme.id, old_status="applied", new_status="interview"))
        s.add(
            Interview(
                application_id=acme.id,
                scheduled_at=utcnow() + timedelta(days=2),
                meeting_link="https://zoom.us/j/1",
                created_at=T0,
                updated_at=T0,
            )
        )
        s.commit()
        return {"acme": acme.id, "evil": evil.id, "flagged": flagged.id}


# --- auth flow -------------------------------------------------------------


def test_root_redirects_to_dashboard(client):
    assert client.get("/").headers["location"] == "/dashboard"
    assert client.head("/").status_code == 303
    assert client.head("/healthz").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard",
        "/dashboard/applications",
        "/dashboard/board",
        "/dashboard/review",
        "/dashboard/applications/1",
    ],
)
def test_pages_redirect_to_login(client, path):
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"] == f"/login?next={path}"


def test_htmx_requests_get_full_page_redirect(client):
    r = client.get("/dashboard/applications", headers=HX)
    assert r.status_code == 204 and r.headers["hx-redirect"].startswith("/login")


def test_login_page_and_errors(client):
    assert "Log in" in client.get("/login").text
    r = client.post("/login", data={"username": "me", "password": "nope", "next": "/dashboard"})
    assert r.status_code == 401 and "Wrong username or password" in r.text


@pytest.mark.parametrize(
    "next_,expected",
    [
        ("/dashboard/board", "/dashboard/board"),
        ("//evil.com", "/dashboard"),
        ("https://evil.com", "/dashboard"),
        ("/login", "/dashboard"),
    ],
)
def test_login_redirect_is_safe(client, next_, expected):
    r = client.post("/login", data={"username": "me", "password": PASSWORD, "next": next_})
    assert r.headers["location"] == expected


def test_logout(authed):
    assert authed.post("/logout").headers["location"] == "/login"
    assert authed.get("/dashboard").status_code == 303


# --- pages -----------------------------------------------------------------


def test_overview(authed, seeded):
    r = authed.get("/dashboard")
    assert r.status_code == 200
    html = r.text
    assert "Applications per week" in html and "Funnel" in html
    assert "Acme" in html and "Join" in html  # upcoming interview
    assert 'id="review-badge" data-count="2"' in html  # one email + one application
    assert "<script>alert(1)</script>" not in html


def test_applications_page_filters_and_escaping(authed, seeded):
    html = authed.get("/dashboard/applications").text
    assert "2 applications" in html
    assert (
        "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert(1)</script>" not in html
    )
    partial = authed.get("/dashboard/applications", params={"status": "interview"}, headers=HX).text
    assert "1 application" in partial and "<html" not in partial and "Acme" in partial
    partial = authed.get("/dashboard/applications", params={"review": "true"}, headers=HX).text
    assert "1 application" in partial and "Acme" not in partial
    assert (
        authed.get("/dashboard/applications", params={"q": "acm"}, headers=HX).text.count("<tr id=")
        == 1
    )


def test_status_change_htmx_and_plain(authed, seeded):
    r = authed.post(
        f"/dashboard/applications/{seeded['acme']}/status", data={"status": "offer"}, headers=HX
    )
    assert r.status_code == 200 and r.text.startswith(f'<tr id="app-{seeded["acme"]}"')
    assert '<option value="offer" selected>' in r.text
    r = authed.post(
        f"/dashboard/applications/{seeded['acme']}/status",
        data={"status": "rejected"},
        headers={"Referer": "http://testserver/dashboard/board"},
    )
    assert r.status_code == 303 and r.headers["location"].endswith("/dashboard/board")
    assert (
        authed.post(
            f"/dashboard/applications/{seeded['acme']}/status", data={"status": "nope"}
        ).status_code
        == 422
    )


def test_board(authed, seeded):
    html = authed.get("/dashboard/board").text
    for status in ("applied", "assessment", "interview", "offer", "rejected"):
        assert f'data-status="{status}"' in html
    assert 'draggable="true"' in html


def test_detail_and_save(authed, seeded):
    html = authed.get(f"/dashboard/applications/{seeded['acme']}").text
    assert "Thanks for applying to Acme" in html and "#all/g-conf" in html
    assert "(manual edit)" in html and "Join link" in html
    r = authed.post(
        f"/dashboard/applications/{seeded['acme']}",
        data={"company": "Acme Corp", "role": "", "applied_date": "2026-09-30", "status": "offer"},
    )
    assert r.headers["location"].endswith("?saved=1")
    html = authed.get(r.headers["location"]).text
    assert "Saved." in html and "Acme Corp" in html and 'value="2026-09-30"' in html
    bad = authed.post(
        f"/dashboard/applications/{seeded['acme']}",
        data={"company": "X", "applied_date": "30/09/2026", "status": "offer"},
    )
    assert bad.status_code == 422
    assert authed.get("/dashboard/applications/9999").status_code == 404


def test_review_flow(authed, seeded):
    html = authed.get("/dashboard/review").text
    assert "An update on your application" in html and "Low confidence" in html
    r = authed.post(
        f"/dashboard/review/emails/{seeded['flagged']}",
        data={"action": "link", "application_id": seeded["acme"]},
        headers=HX,
    )
    assert r.status_code == 200 and 'hx-swap-oob="true"' in r.text and 'data-count="1"' in r.text
    r = authed.post(f"/dashboard/review/applications/{seeded['evil']}", headers=HX)
    assert 'data-count="0"' in r.text
    assert "Nothing to review" in authed.get("/dashboard/review").text
    assert authed.get(f"/dashboard/applications/{seeded['acme']}").text.count("Rejected") >= 1


def test_review_invalid_action(authed, seeded):
    r = authed.post(f"/dashboard/review/emails/{seeded['flagged']}", data={"action": "explode"})
    assert r.status_code == 422


# --- origin protection & assets --------------------------------------------


def test_cross_origin_writes_blocked(authed, seeded):
    url = f"/dashboard/applications/{seeded['acme']}/status"
    assert (
        authed.post(
            url, data={"status": "offer"}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        authed.post(
            url, data={"status": "offer"}, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    assert (
        authed.patch(
            f"/applications/{seeded['acme']}",
            json={"status": "offer"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    ok = authed.post(url, data={"status": "offer"}, headers={"Origin": "http://testserver", **HX})
    assert ok.status_code == 200


def test_assets_are_cache_busted(client):
    html = client.get("/login").text
    assert "/static/app.css?v=" in html and "/static/htmx.min.js?v=" in html


def test_static_assets(client):
    assert client.get("/static/htmx.min.js").status_code == 200
    assert "--st-interview" in client.get("/static/app.css").text


# --- chart geometry --------------------------------------------------------


def test_weekly_bars_geometry():
    weeks = [(date(2026, 8, 3) + timedelta(weeks=i), v) for i, v in enumerate([0, 3, 10, 7])]
    chart = weekly_bars(weeks, width=400, height=200)
    assert chart.max_value == 10
    assert chart.bars[0].path == "" and chart.bars[2].y == pytest.approx(8)
    for a, b in zip(chart.bars, chart.bars[1:], strict=False):
        assert b.x - (a.x + a.width) >= 2  # surface gap between bars
    assert all(bar.y + bar.height == pytest.approx(chart.baseline) for bar in chart.bars)


@pytest.mark.parametrize("value,expected", [(0, 4), (3, 4), (7, 10), (35, 40), (101, 150)])
def test_nice_max(value, expected):
    assert nice_max(value) == expected


# --- side panel ------------------------------------------------------------


@pytest.fixture
def mixed(session_factory):
    with session_factory() as s:
        rows = [
            ("Acme", "SWE", "applied", T0),
            ("Globex", "ML", "applied", T0),
            ("Initech", "DS", "interview", T0),
            ("Hooli", "PM", "rejected", T0 + timedelta(days=2)),
            ("Umbrella", None, "rejected", T0 + timedelta(days=5)),
        ]
        for company, role, st, at in rows:
            s.add(
                Application(
                    company=company,
                    role=role,
                    status=st,
                    last_update=at,
                    status_changed_at=at,
                    source="t",
                    needs_review=False,
                )
            )
        s.commit()


@pytest.mark.parametrize(
    "path", ["/dashboard", "/dashboard/applications", "/dashboard/board", "/dashboard/review"]
)
def test_side_panel_on_every_page(authed, mixed, path):
    html = authed.get(path).text
    assert 'class="side"' in html
    assert 'Total applications</span><strong class="num">5</strong>' in html
    assert "Still in play" in html and '<strong class="num">3</strong>' in html
    assert "Rejections (2)" in html
    # newest rejection first, each linking to its application
    assert html.index(">Umbrella</a>") < html.index(">Hooli</a>")
    assert "PM · " in html


def test_side_panel_counts_link_to_filtered_tables(authed, mixed):
    html = authed.get("/dashboard").text
    assert 'href="/dashboard/applications?status=rejected"' in html
    active = authed.get(
        "/dashboard/applications",
        params=[("status", s) for s in ("applied", "assessment", "interview", "offer")],
        headers=HX,
    ).text
    assert "3 applications" in active and "Hooli" not in active


def test_side_panel_not_in_partials_or_login(authed, mixed, client):
    partial = authed.get("/dashboard/applications", headers=HX).text
    assert 'class="side"' not in partial
    assert 'class="side"' not in client.get("/login").text


def test_side_panel_empty_state(authed):
    html = authed.get("/dashboard").text
    assert "Rejections (0)" in html and "No rejections yet." in html
