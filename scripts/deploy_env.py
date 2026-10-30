"""Print the environment variables your server needs, built from your local .env and
credentials.json, ready to paste into Railway's (or Render's) bulk variable editor.

It prints secrets, so send it straight to the clipboard instead of the screen:

    python -m scripts.deploy_env --url https://YOUR-APP.up.railway.app | pbcopy
"""

import argparse
import json
import sys

from app.config import Settings

# Everything the server needs. Token file paths and the local OAuth port are not needed there:
# inbox tokens live (encrypted) in the database.
KEYS = [
    "DATABASE_URL",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CLASSIFIER_MODEL",
    "CLAUDE_WRITER_MODEL",
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD_HASH",
    "SESSION_SECRET",
    "TOKEN_ENCRYPTION_KEY",
    "SLACK_WEBHOOK_URL",
    "TIMEZONE",
    "LOG_LEVEL",
    "DIGEST_HOUR",
    "POLL_INTERVAL_MINUTES",
    "INITIAL_BACKFILL_DAYS",
    "CLASSIFIER_CONFIDENCE_THRESHOLD",
    "EMBEDDINGS_ENABLED",
    "OUTREACH_DAILY_CAP",
    "OUTREACH_MIN_GAP_SECONDS",
    "OUTREACH_MAX_GAP_SECONDS",
    "OUTREACH_DEDUPE_DAYS",
    "FOLLOWUP_BUSINESS_DAYS",
]
REQUIRED = {
    "DATABASE_URL",
    "ANTHROPIC_API_KEY",
    "DASHBOARD_PASSWORD_HASH",
    "SESSION_SECRET",
    "TOKEN_ENCRYPTION_KEY",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="the app's public https:// address")
    args = parser.parse_args()
    if not args.url.startswith("https://"):
        sys.exit("--url must be the https:// address your host gives the app")

    settings = Settings()
    missing = []
    lines = []
    for key in KEYS:
        value = getattr(settings, key.lower())
        if value is None:
            if key in REQUIRED:
                missing.append(key)
            continue
        value = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        lines.append(f"{key}={value}")
    if missing:
        sys.exit("Set these locally first: " + ", ".join(missing))

    client_file = settings.resolve(settings.gmail_credentials_file)
    if client_file.exists():
        lines.append("GOOGLE_OAUTH_CLIENT_JSON=" + json.dumps(json.loads(client_file.read_text())))
    lines += [
        "APP_ENV=production",
        f"PUBLIC_BASE_URL={args.url.rstrip('/')}",
        "SCHEDULER_ENABLED=true",
    ]
    print("\n".join(lines))
    print(f"Prepared {len(lines)} variables (not shown).", file=sys.stderr)


if __name__ == "__main__":
    main()
