"""Shared FastAPI dependencies: DB session and the logged-in-user guard."""

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import Settings

SESSION_USER_KEY = "user"


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    if factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database is not configured")
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def require_user(request: Request, settings: Settings = Depends(get_settings_dep)) -> str:
    user = request.session.get(SESSION_USER_KEY)
    if user is None or user != settings.dashboard_username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    return user
