"""Incremental Gmail fetcher.

Normal runs read only what changed since the stored historyId (users.history.list). Two bounded
fallbacks exist, neither a full rescan:
- bootstrap: no cursor yet -> messages newer than INITIAL_BACKFILL_DAYS
- catchup: Gmail expired the cursor (HTTP 404) -> messages newer than the last successful run

Only read calls are made: getProfile, history.list, messages.list, messages.get.
The caller saves the returned history_id after it has processed the messages, so a crash
mid-run re-fetches rather than skips (processing is idempotent on gmail_id).
"""

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from googleapiclient.errors import HttpError

from app.gmail.message import EmailMessage, parse_message
from app.gmail.prefilter import gmail_search_query
from app.gmail.sync_state import SyncState

log = logging.getLogger(__name__)

METADATA_HEADERS = ["From", "Subject", "Date"]
PAGE_SIZE = 500
# Gmail allows 250 quota units/s per user; messages.get costs 5, so ~20 calls/s leaves headroom.
PACE_SECONDS = 0.05
RETRIES = 5

FetchMode = Literal["bootstrap", "incremental", "catchup"]


@dataclass(frozen=True)
class FetchResult:
    messages: list[EmailMessage]  # metadata only (no body)
    history_id: str
    mode: FetchMode


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HistoryExpired(Exception):
    pass


class GmailFetcher:
    def __init__(
        self,
        service,
        *,
        initial_backfill_days: int = 30,
        now: Callable[[], datetime] = _utcnow,
        pace_seconds: float = PACE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._users = service.users()
        self.initial_backfill_days = initial_backfill_days
        self._now = now
        self._pace = pace_seconds
        self._sleep = sleep
        self._last_call = 0.0

    def _execute(self, request):
        """Space calls out to stay under Gmail's per-user rate limit, and let the client retry
        429/5xx and rate-limit 403s with exponential backoff."""
        wait = self._pace - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()
        return request.execute(num_retries=RETRIES)

    def fetch_new(self, state: SyncState) -> FetchResult:
        mode: FetchMode
        if state.last_history_id is None:
            mode = "bootstrap"
            history_id = self._current_history_id()
            ids = list(
                self._list_ids_since(self._now() - timedelta(days=self.initial_backfill_days))
            )
        else:
            try:
                mode = "incremental"
                ids, history_id = self._history_ids(state.last_history_id)
            except HistoryExpired:
                mode = "catchup"
                log.warning("Stored historyId expired; catching up from last run time")
                history_id = self._current_history_id()
                since = state.last_run_at or self._now() - timedelta(
                    days=self.initial_backfill_days
                )
                ids = list(self._list_ids_since(since))

        messages = [m for m in (self.get_metadata(i) for i in _dedupe(ids)) if m is not None]
        log.info("Fetched %d messages (mode=%s)", len(messages), mode)
        return FetchResult(messages=messages, history_id=history_id, mode=mode)

    def preview(self, days: int) -> list[EmailMessage]:
        """Metadata for messages from the last `days` days; touches no cursor."""
        ids = self._list_ids_since(self._now() - timedelta(days=days), narrow=False)
        return [m for m in (self.get_metadata(i) for i in _dedupe(ids)) if m is not None]

    def get_metadata(self, gmail_id: str) -> EmailMessage | None:
        return self._get(gmail_id, format="metadata", metadataHeaders=METADATA_HEADERS)

    def get_full(self, gmail_id: str) -> EmailMessage | None:
        return self._get(gmail_id, format="full")

    def _get(self, gmail_id: str, **params) -> EmailMessage | None:
        try:
            raw = self._execute(self._users.messages().get(userId="me", id=gmail_id, **params))
        except HttpError as e:
            if e.resp.status == 404:  # deleted between listing and fetching
                return None
            raise
        return parse_message(raw)

    def _current_history_id(self) -> str:
        return str(self._execute(self._users.getProfile(userId="me"))["historyId"])

    def _history_ids(self, start_history_id: str) -> tuple[list[str], str]:
        ids: list[str] = []
        page_token = None
        while True:
            try:
                resp = self._execute(
                    self._users.history().list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        maxResults=PAGE_SIZE,
                        pageToken=page_token,
                    )
                )
            except HttpError as e:
                if e.resp.status == 404:
                    raise HistoryExpired from e
                raise
            for record in resp.get("history", []):
                ids.extend(m["message"]["id"] for m in record.get("messagesAdded", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return ids, str(resp["historyId"])

    def _list_ids_since(self, since: datetime, *, narrow: bool = True) -> Iterator[str]:
        query = f"after:{int(since.timestamp())}"
        if narrow:
            query += " " + gmail_search_query()
        page_token = None
        while True:
            resp = self._execute(
                self._users.messages().list(
                    userId="me", q=query, maxResults=PAGE_SIZE, pageToken=page_token
                )
            )
            yield from (m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return


def _dedupe(ids) -> list[str]:
    return list(dict.fromkeys(ids))
