from datetime import UTC, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from app.classifier.schema import EmailType
from app.db.matcher import Candidate, Matcher, fuzzy_match, pick_vector_match
from app.db.models import Application, Base, Email, Status
from app.db.state_machine import TYPE_TO_STATUS, transition
from app.db.sync_state import DbSyncStateStore
from app.gmail.sync_state import SyncState
from tests.conftest import alembic_config

T0 = datetime(2026, 10, 20, 12, tzinfo=UTC)


# --- migrations ------------------------------------------------------------


def test_migration_matches_models(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_migration_downgrades_cleanly():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
        command.downgrade(alembic_config(conn), "base")
        assert set(inspect(conn).get_table_names()) == {"alembic_version"}


def test_naive_datetimes_rejected(session):
    session.add(
        Application(
            company="X",
            status="applied",
            source="manual",
            last_update=datetime(2026, 1, 1),
            status_changed_at=T0,
        )
    )
    with pytest.raises(Exception, match="naive datetime"):
        session.flush()


def test_status_check_constraint(session):
    session.add(
        Application(
            company="X", status="ghosted", source="manual", last_update=T0, status_changed_at=T0
        )
    )
    with pytest.raises(Exception, match="CHECK constraint"):
        session.flush()


def test_db_sync_state_round_trip(session_factory):
    store = DbSyncStateStore(session_factory)
    assert store.load() == SyncState()
    store.save(SyncState(last_history_id="123", last_run_at=T0))
    store.save(SyncState(last_history_id="456", last_run_at=T0 + timedelta(hours=1)))
    assert store.load() == SyncState(last_history_id="456", last_run_at=T0 + timedelta(hours=1))


# --- state machine ---------------------------------------------------------

APP, ASM, INT, OFR, REJ = (
    Status.APPLIED,
    Status.ASSESSMENT,
    Status.INTERVIEW,
    Status.OFFER,
    Status.REJECTED,
)


@pytest.mark.parametrize(
    "current,target,expected,reason",
    [
        (None, APP, APP, "created"),
        (None, INT, INT, "created"),
        (APP, ASM, ASM, "advanced"),
        (APP, INT, INT, "advanced"),  # skipping a stage is fine
        (ASM, INT, INT, "advanced"),
        (INT, OFR, OFR, "advanced"),
        (APP, REJ, REJ, "rejected"),
        (INT, REJ, REJ, "rejected"),
        (OFR, REJ, REJ, "rejected"),
        (INT, APP, None, "backward"),
        (OFR, INT, None, "backward"),
        (INT, ASM, None, "backward"),
        (INT, INT, None, "same_status"),
        (REJ, REJ, None, "same_status"),
    ],
)
def test_transitions(current, target, expected, reason):
    t = transition(current, target, email_at=T0, status_changed_at=T0 - timedelta(days=1))
    assert (t.new_status, t.reason, t.needs_review) == (expected, reason, False)


@pytest.mark.parametrize("target", [APP, ASM, INT, OFR, REJ])
def test_older_email_never_changes_status(target):
    t = transition(INT, target, email_at=T0 - timedelta(hours=1), status_changed_at=T0)
    assert t.new_status is None and t.reason == "older_email"


def test_email_at_same_instant_is_not_older():
    assert transition(APP, INT, email_at=T0, status_changed_at=T0).new_status is INT


@pytest.mark.parametrize("target", [ASM, INT, OFR])
def test_activity_after_rejection_needs_review(target):
    t = transition(REJ, target, email_at=T0, status_changed_at=T0 - timedelta(days=1))
    assert t.new_status is None and t.needs_review


def test_type_mapping_excludes_non_status_types():
    assert EmailType.RECRUITER_OUTREACH not in TYPE_TO_STATUS
    assert EmailType.OTHER not in TYPE_TO_STATUS


# --- matcher ---------------------------------------------------------------


def C(id, company, role=None, status="applied"):
    return Candidate(id, company, role, status)


def test_fuzzy_no_company():
    assert fuzzy_match(None, "SWE", [C(1, "Acme")]).reason == "no_company"


def test_fuzzy_unknown_company():
    r = fuzzy_match("Globex", "SWE", [C(1, "Acme", "SWE")])
    assert r.application_id is None and not r.ambiguous and r.reason == "no_company_match"


def test_fuzzy_company_variants_and_role():
    r = fuzzy_match(
        "Databricks, Inc.",
        "Machine Learning Engineer (R-12345)",
        [C(1, "Databricks", "Machine Learning Engineer"), C(2, "Databricks", "Data Engineer")],
    )
    assert (r.application_id, r.method) == (1, "fuzzy")


def test_fuzzy_single_app_no_role_in_email():
    assert fuzzy_match("Chime", None, [C(7, "Chime", "MLE")]).application_id == 7


def test_fuzzy_multiple_apps_no_role_is_ambiguous():
    r = fuzzy_match("Chime", None, [C(1, "Chime", "MLE"), C(2, "Chime", "SWE")])
    assert r.ambiguous and r.reason == "multiple_candidates"


def test_fuzzy_prefers_the_only_active_application():
    r = fuzzy_match("Chime", None, [C(1, "Chime", "MLE", "rejected"), C(2, "Chime", "SWE")])
    assert r.application_id == 2


def test_fuzzy_role_mismatch_is_ambiguous():
    r = fuzzy_match("Chime", "Product Designer", [C(1, "Chime", "Machine Learning Engineer")])
    assert r.ambiguous and r.reason == "role_mismatch"


def test_fuzzy_roleless_application_links():
    assert fuzzy_match("Chime", "MLE", [C(1, "Chime", None)]).application_id == 1


def test_fuzzy_two_identical_roles_is_ambiguous():
    r = fuzzy_match("Acme", "SWE", [C(1, "Acme", "SWE"), C(2, "Acme", "SWE")])
    assert r.ambiguous


@pytest.mark.parametrize(
    "neighbors,expected_app,ambiguous",
    [
        ([], None, False),
        ([(1, 0.30)], None, False),
        ([(1, 0.05)], 1, False),
        ([(1, 0.05), (1, 0.06), (2, 0.20)], 1, False),
        ([(1, 0.05), (2, 0.08)], None, True),
    ],
)
def test_pick_vector_match(neighbors, expected_app, ambiguous):
    r = pick_vector_match(neighbors)
    assert (r.application_id, r.ambiguous) == (expected_app, ambiguous)


def _app(session, company, role=None, status="applied"):
    app = Application(
        company=company,
        role=role,
        status=status,
        source="test",
        last_update=T0,
        status_changed_at=T0,
    )
    session.add(app)
    session.flush()
    return app


def test_matcher_thread_beats_fuzzy(session):
    a = _app(session, "Acme", "SWE")
    b = _app(session, "Globex", "SWE")
    session.add(
        Email(
            gmail_id="g1",
            thread_id="t1",
            application_id=b.id,
            type="application_confirmation",
            received_at=T0,
            confidence=0.9,
            sender_email="x@globex.com",
            subject="s",
        )
    )
    session.flush()
    r = Matcher().match(session, thread_id="t1", company="Acme", role="SWE", embedding=None)
    assert (r.application_id, r.method) == (b.id, "thread")
    r = Matcher().match(session, thread_id="t2", company="Acme", role="SWE", embedding=None)
    assert (r.application_id, r.method) == (a.id, "fuzzy")


def test_matcher_skips_vector_on_sqlite(session):
    _app(session, "Acme")
    r = Matcher().match(session, thread_id="t", company=None, role=None, embedding=[0.1] * 384)
    assert r.application_id is None and r.method == "none"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+psycopg://u:p@h/db?sslmode=require"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite:///x.db"),
    ],
)
def test_normalize_database_url(url, expected):
    from app.db.session import normalize_url

    assert normalize_url(url) == expected
