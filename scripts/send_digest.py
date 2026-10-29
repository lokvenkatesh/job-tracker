"""Daily digest and Slack checks.

python -m scripts.send_digest --preview   # print today's digest here; sends nothing
python -m scripts.send_digest --test      # send a short test message to Slack
python -m scripts.send_digest             # send the digest to Slack now
"""

import argparse
from datetime import UTC, datetime

from app.config import get_settings
from app.db.session import database_url, make_engine, make_session_factory, session_scope
from app.notify.base import build_notifier
from app.notify.digest import build_digest, digest_text
from app.notify.slack import SlackNotifier
from app.pipeline.scheduler import send_digest

TEST_MESSAGE = "Job tracker is connected. Interview alerts and the daily digest will arrive here."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--test", action="store_true")
    args = parser.parse_args()
    settings = get_settings()

    if args.preview:
        engine = make_engine(database_url(settings))
        with session_scope(make_session_factory(engine)) as session:
            digest = build_digest(session, now=datetime.now(UTC))
        print(digest_text(digest, settings.timezone))
        return

    notifier = build_notifier(settings)
    if not isinstance(notifier, SlackNotifier):
        raise SystemExit(
            "SLACK_WEBHOOK_URL is empty. Run: python -m scripts.set_secret SLACK_WEBHOOK_URL"
        )
    if args.test:
        notifier.post({"text": TEST_MESSAGE})
        print("Test message sent.")
        return
    send_digest(settings, notifier)
    print("Digest sent.")


if __name__ == "__main__":
    main()
