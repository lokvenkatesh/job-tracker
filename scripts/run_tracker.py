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
    s = run_tracker_job(settings)
    print(
        f"\nmode={s.mode} fetched={s.fetched} classified={s.queued} alerts={s.alerts_sent}\n"
        + "\n".join(f"  {action}: {n}" for action, n in sorted(s.actions.items()))
    )


if __name__ == "__main__":
    main()
