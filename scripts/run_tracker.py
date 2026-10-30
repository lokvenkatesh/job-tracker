"""Run one tracker pass now (what the hourly job does): fetch -> prefilter -> classify -> match ->
update_db -> notify. Prints a summary; never prints email bodies.

    python -m scripts.run_tracker
"""

import logging

from app.config import get_settings
from app.pipeline.scheduler import missing_config, run_tracker_job


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if missing := missing_config(settings):
        raise SystemExit("Missing: " + ", ".join(missing))
    results = run_tracker_job(settings)
    if not results:
        print("\nNo Gmail inbox connected. Add one on the dashboard's Inboxes page.")
    for email, s in results:
        if isinstance(s, str):
            print(f"\n{email}: {s}")
            continue
        print(
            f"\n{email}: mode={s.mode} fetched={s.fetched} classified={s.queued} "
            f"alerts={s.alerts_sent}\n"
            + "\n".join(f"  {action}: {n}" for action, n in sorted(s.actions.items()))
        )


if __name__ == "__main__":
    main()
