"""Hourly tracker job (APScheduler). The daily digest job joins in step 5."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import Engine, func, select, text

from app.classifier.classifier import EmailClassifier
from app.config import Settings, get_settings
from app.db.models import OutreachCampaign, OutreachEmail, OutreachStatus
from app.db.session import database_url, make_engine, make_session_factory, session_scope
from app.db.sync_state import DbSyncStateStore
from app.embeddings import FastEmbedEmbedder
from app.gmail.auth import get_readonly_service, get_send_service
from app.gmail.fetcher import GmailFetcher
from app.notify.base import build_notifier
from app.notify.digest import build_digest, digest_message
from app.notify.slack import SlackNotifier
from app.outreach.followups import create_followups
from app.outreach.generator import DraftGenerator
from app.outreach.sender import SendResult, send_next
from app.pipeline.tracker import RunSummary, TrackerDeps, run_once

log = logging.getLogger(__name__)

TRACKER_LOCK_ID = 7_340_001  # arbitrary, app-wide constants for pg_try_advisory_lock
DIGEST_LOCK_ID = 7_340_002
SENDER_LOCK_ID = 7_340_003
FOLLOWUP_LOCK_ID = 7_340_004


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
def tracker_lock(engine: Engine, lock_id: int = TRACKER_LOCK_ID) -> Iterator[None]:
    """Postgres advisory lock so two app instances never run the same job concurrently."""
    if engine.dialect.name != "postgresql":
        yield
        return
    with engine.connect() as conn:
        if not conn.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}):
            raise TrackerAlreadyRunning(f"another process holds lock {lock_id}")
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})


def build_deps(settings: Settings, engine: Engine) -> TrackerDeps:
    return TrackerDeps(
        fetcher=GmailFetcher(
            get_readonly_service(settings), initial_backfill_days=settings.initial_backfill_days
        ),
        classifier=EmailClassifier(settings=settings),
        session_factory=make_session_factory(engine),
        sync_store=DbSyncStateStore(make_session_factory(engine)),
        notifier=build_notifier(settings),
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


def send_digest(settings: Settings, notifier: SlackNotifier, *, now: datetime | None = None):
    engine = make_engine(database_url(settings))
    try:
        with (
            tracker_lock(engine, DIGEST_LOCK_ID),
            session_scope(make_session_factory(engine)) as session,
        ):
            digest = build_digest(session, now=now or datetime.now(UTC))
        notifier.post(digest_message(digest, settings.timezone))
    finally:
        engine.dispose()
    log.info("Sent daily digest")
    return digest


def run_digest_job(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    notifier = build_notifier(settings)
    if not isinstance(notifier, SlackNotifier):
        log.warning("Daily digest skipped: SLACK_WEBHOOK_URL is empty")
        return
    send_digest(settings, notifier)


def send_ready(settings: Settings) -> bool:
    return settings.resolve(settings.gmail_send_token_file).exists()


def run_sender_job(settings: Settings | None = None) -> SendResult | None:
    """One tick of the throttled sender: sends at most one approved, queued email."""
    settings = settings or get_settings()
    if settings.database_url is None:
        return None
    engine = make_engine(database_url(settings))
    try:
        with tracker_lock(engine, SENDER_LOCK_ID):
            factory = make_session_factory(engine)
            with factory() as db:
                queued = db.scalar(
                    select(func.count())
                    .select_from(OutreachEmail)
                    .join(OutreachCampaign, OutreachEmail.campaign_id == OutreachCampaign.id)
                    .where(
                        OutreachEmail.status == OutreachStatus.APPROVED.value,
                        OutreachCampaign.send_requested_at.is_not(None),
                    )
                )
                if not queued:
                    return None
                if not send_ready(settings):
                    log.warning(
                        "Emails are queued but Gmail sending isn't connected (token_send.json)"
                    )
                    return None
                result = send_next(
                    db,
                    send_service=get_send_service(settings),
                    read_service=get_readonly_service(settings),
                    settings=settings,
                )
                db.commit()
                return result
    except TrackerAlreadyRunning:
        return None
    finally:
        engine.dispose()


def run_followups_job(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    if settings.database_url is None or settings.anthropic_api_key is None:
        return 0
    engine = make_engine(database_url(settings))
    try:
        with tracker_lock(engine, FOLLOWUP_LOCK_ID):
            return create_followups(
                make_session_factory(engine),
                DraftGenerator(settings=settings),
                timezone=settings.timezone,
                business_days=settings.followup_business_days,
            )
    except TrackerAlreadyRunning:
        return 0
    finally:
        engine.dispose()


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
    scheduler.add_job(
        run_digest_job,
        "cron",
        hour=settings.digest_hour,
        minute=0,
        id="digest",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_sender_job, "interval", seconds=30, id="sender", max_instances=1, coalesce=True
    )
    scheduler.add_job(
        run_followups_job,
        "interval",
        hours=1,
        id="followups",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC) + timedelta(minutes=5),
    )
    scheduler.start()
    log.info(
        "Tracker every %d min; digest daily at %02d:00 %s",
        settings.poll_interval_minutes,
        settings.digest_hour,
        settings.timezone,
    )
    return scheduler
