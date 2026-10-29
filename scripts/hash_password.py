"""Set the dashboard password: prompts (hidden), stores its bcrypt hash in .env as
DASHBOARD_PASSWORD_HASH, and creates SESSION_SECRET if it's empty.

    python -m scripts.hash_password
"""

import getpass
import os
import secrets
import shutil

import bcrypt

from app.config import Settings
from scripts.set_secret import ENV_PATH, EXAMPLE_PATH, set_value

MIN_LENGTH = 10


def main() -> None:
    password = getpass.getpass("Dashboard password (input hidden): ")
    if len(password) < MIN_LENGTH:
        raise SystemExit(f"Use at least {MIN_LENGTH} characters. .env unchanged.")
    if password != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords do not match. .env unchanged.")

    if not ENV_PATH.exists():
        shutil.copy(EXAMPLE_PATH, ENV_PATH)
    text = ENV_PATH.read_text()
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    # Single quotes stop .env parsers from treating the hash's `$` as variable references.
    text = set_value(text, "DASHBOARD_PASSWORD_HASH", f"'{hashed}'")
    if Settings(_env_file=ENV_PATH).session_secret is None:
        text = set_value(text, "SESSION_SECRET", secrets.token_urlsafe(48))
        print("Created SESSION_SECRET.")
    ENV_PATH.write_text(text)
    os.chmod(ENV_PATH, 0o600)
    username = Settings(_env_file=ENV_PATH).dashboard_username
    print(f"Saved password for dashboard user '{username}'.")


if __name__ == "__main__":
    main()
