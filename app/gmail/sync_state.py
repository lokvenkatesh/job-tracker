"""Where the fetcher's cursor (Gmail historyId) is stored. A Postgres-backed store replaces the JSON
file in step 3; both satisfy SyncStateStore."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SyncState:
    last_history_id: str | None = None
    last_run_at: datetime | None = None


class SyncStateStore(Protocol):
    def load(self) -> SyncState: ...

    def save(self, state: SyncState) -> None: ...


class InMemorySyncStateStore:
    def __init__(self, state: SyncState | None = None) -> None:
        self.state = state or SyncState()

    def load(self) -> SyncState:
        return self.state

    def save(self, state: SyncState) -> None:
        self.state = state


class JsonFileSyncStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SyncState:
        if not self.path.exists():
            return SyncState()
        raw = json.loads(self.path.read_text())
        run_at = raw.get("last_run_at")
        return SyncState(
            last_history_id=raw.get("last_history_id"),
            last_run_at=datetime.fromisoformat(run_at) if run_at else None,
        )

    def save(self, state: SyncState) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "last_history_id": state.last_history_id,
                    "last_run_at": state.last_run_at.isoformat() if state.last_run_at else None,
                }
            )
        )
