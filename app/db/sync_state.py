"""Postgres-backed SyncStateStore (single row, id=1)."""

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import MailAccount, SyncStateRow
from app.db.session import session_scope
from app.gmail.sync_state import SyncState


class DbSyncStateStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def load(self) -> SyncState:
        with session_scope(self._factory) as session:
            row = session.get(SyncStateRow, 1)
            if row is None:
                return SyncState()
            return SyncState(last_history_id=row.last_history_id, last_run_at=row.last_run_at)

    def save(self, state: SyncState) -> None:
        with session_scope(self._factory) as session:
            row = session.get(SyncStateRow, 1) or SyncStateRow(id=1)
            row.last_history_id = state.last_history_id
            row.last_run_at = state.last_run_at
            session.add(row)


class AccountSyncStateStore:
    """The Gmail historyId cursor for one connected inbox."""

    def __init__(self, session_factory: sessionmaker[Session], account_id: int) -> None:
        self._factory = session_factory
        self.account_id = account_id

    def load(self) -> SyncState:
        with session_scope(self._factory) as session:
            account = session.get(MailAccount, self.account_id)
            return SyncState(
                last_history_id=account.last_history_id, last_run_at=account.last_run_at
            )

    def save(self, state: SyncState) -> None:
        with session_scope(self._factory) as session:
            account = session.get(MailAccount, self.account_id)
            account.last_history_id = state.last_history_id
            account.last_run_at = state.last_run_at
