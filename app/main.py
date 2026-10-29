"""FastAPI entrypoint. Run with: uvicorn app.main:app --reload"""

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app.api import applications, auth
from app.config import Settings, get_settings
from app.db.session import database_url, make_engine, make_session_factory

log = logging.getLogger(__name__)

SESSION_MAX_AGE = 14 * 24 * 3600


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    scheduler = None
    if settings.scheduler_enabled:
        from app.pipeline.scheduler import start_scheduler

        scheduler = start_scheduler(settings)
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


def create_app(
    settings: Settings | None = None, session_factory: sessionmaker[Session] | None = None
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if session_factory is None and settings.database_url is not None:
        session_factory = make_session_factory(make_engine(database_url(settings)))

    app = FastAPI(title="Job Application Tracker", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.login_throttle = auth.LoginThrottle()

    if settings.session_secret is not None:
        secret = settings.session_secret.get_secret_value()
    else:
        log.warning("SESSION_SECRET is empty; using a random one (logins reset on restart)")
        secret = secrets.token_urlsafe(48)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="jt_session",
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.app_env == "production",
    )

    app.include_router(auth.router)
    app.include_router(applications.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def __getattr__(name: str):
    # `uvicorn app.main:app` builds the app lazily, so importing this module (e.g. in tests)
    # doesn't read .env or connect anywhere.
    if name == "app":
        return create_app()
    raise AttributeError(name)
