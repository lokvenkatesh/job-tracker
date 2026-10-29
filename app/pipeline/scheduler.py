"""Hourly tracker job (APScheduler). The daily digest job joins in step 5."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import Engine, text

from app.classifier.classifier import EmailClassifier
from app.config import Settings, get_settings
from app.db.session import database_url, make_engine, make_session_factory
from app.db.sync_state import DbSyncStateStore
from app.embeddings import FastEmbedEmbedder
from app.gmail.auth import get_readonly_service
from app.gmail.fetcher import GmailFetcher
from app.notify.base import LogNotifier
from app.pipeline.tracker import RunSummary, TrackerDeps, run_once

log = logging.getLogger(__name__)

TRACKER_LOCK_ID = 7_340_001  # arbitrary, app-wide constant for pg_try_advisory_lock


class TrackerAlreadyRunning(RuntimeError):
    pass


def missing_config(settings: Settings) -> list[str]:
    missing = []
    if settings.database_url is None:
        missing.append("DATABASE_URL")
    if settings.anthropic_api_key is None:
        missing.append("ANTHROPIC_API_KEY")
    if not settings.resolve(settings.gmail_readonly_token_file).exists():
        missing.append(settings.gmail_readonly_token_file.name)
    return missing


@contextmanager
def tracker_lock(engine: Engine) -> Iterator[None]:
    """Postgres advisory lock so two app instances never run the tracker concurrently."""
    if engine.dialect.name != "postgresql":
        yield
        return
    with engine.connect() as conn:
        if not conn.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": TRACKER_LOCK_ID}):
            raise TrackerAlreadyRunning("another tracker run holds the lock")
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": TRACKER_LOCK_ID})


def build_deps(settings: Settings, engine: Engine) -> TrackerDeps:
    return TrackerDeps(
        fetcher=GmailFetcher(
            get_readonly_service(settings), initial_backfill_days=settings.initial_backfill_days
        ),
        classifier=EmailClassifier(settings=settings),
        session_factory=make_session_factory(engine),
        sync_store=DbSyncStateStore(make_session_factory(engine)),
        notifier=LogNotifier(),
        settings=settings,
        embedder=FastEmbedEmbedder() if settings.embeddings_enabled else None,
    )


def run_tracker_job(settings: Settings | None = None) -> RunSummary:
    settings = settings or get_settings()
    engine = make_engine(database_url(settings))
    try:
        with tracker_lock(engine):
            summary = run_once(build_deps(settings, engine))
    finally:
        engine.dispose()
    log.info("Tracker run: %s", summary)
    return summary


def start_scheduler(settings: Settings | None = None) -> BackgroundScheduler | None:
    settings = settings or get_settings()
    if missing := missing_config(settings):
        log.warning("Tracker scheduler not started; missing: %s", ", ".join(missing))
        return None
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        run_tracker_job,
        "interval",
        minutes=settings.poll_interval_minutes,
        id="tracker",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=30),
    )
    scheduler.start()
    log.info("Tracker scheduled every %d min", settings.poll_interval_minutes)
    return scheduler
