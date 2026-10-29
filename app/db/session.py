"""Engine and session factory."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings


class DatabaseNotConfigured(RuntimeError):
    pass


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_recycle=300)


@lru_cache
def _default_engine() -> Engine:
    return make_engine(database_url(get_settings()))


def database_url(settings: Settings) -> str:
    if settings.database_url is None:
        raise DatabaseNotConfigured(
            "DATABASE_URL is empty. Run: python -m scripts.set_secret DATABASE_URL"
        )
    return normalize_url(settings.database_url.get_secret_value())


def normalize_url(url: str) -> str:
    """Accept the postgres:// / postgresql:// strings Neon and Supabase hand out."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or _default_engine(), expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
