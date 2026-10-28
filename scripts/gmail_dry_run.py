"""Read-only check of the fetcher + prefilter against your real inbox. Prints sender, subject
and the prefilter decision per message; never prints bodies.

    python -m scripts.gmail_dry_run --preview-days 14   # look back 14 days, no cursor change
    python -m scripts.gmail_dry_run                     # incremental run using .sync_state.json
"""

import argparse
import logging
from collections import Counter
from datetime import UTC, datetime

from app.config import PROJECT_ROOT, get_settings
from app.gmail.auth import get_readonly_service
from app.gmail.fetcher import GmailFetcher
from app.gmail.prefilter import prefilter
from app.gmail.sync_state import JsonFileSyncStateStore, SyncState


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-days", type=int, help="look back N days without touching state")
    parser.add_argument("--show-dropped", action="store_true", help="also list dropped messages")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = get_settings()
    fetcher = GmailFetcher(
        get_readonly_service(settings), initial_backfill_days=settings.initial_backfill_days
    )
    store = JsonFileSyncStateStore(PROJECT_ROOT / ".sync_state.json")

    if args.preview_days:
        messages = fetcher.preview(args.preview_days)
    else:
        result = fetcher.fetch_new(store.load())
        messages = result.messages
        store.save(SyncState(last_history_id=result.history_id, last_run_at=datetime.now(UTC)))
        print(f"mode={result.mode} new_history_id={result.history_id}")

    reasons: Counter[str] = Counter()
    for msg in messages:
        decision = prefilter(msg)
        reasons[decision.reason.split(":")[0] if not decision.passed else "passed"] += 1
        if decision.passed or args.show_dropped:
            mark = "PASS" if decision.passed else "drop"
            print(
                f"{mark}  {msg.received_at:%Y-%m-%d}  {msg.sender_email[:32]:<32}  "
                f"{msg.subject[:60]:<60}  {decision.reason}"
            )
    print(f"\n{len(messages)} messages: " + ", ".join(f"{k}={v}" for k, v in reasons.items()))


if __name__ == "__main__":
    main()
