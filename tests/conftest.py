import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from alembic import command
from app.config import PROJECT_ROOT


def alembic_config(connection) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture
def engine():
    """In-memory SQLite with the real migrations applied."""
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    with eng.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s
