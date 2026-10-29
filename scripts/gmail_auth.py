"""One-time Gmail consent. Usage:
python -m scripts.gmail_auth readonly   # tracker token (token.json)
python -m scripts.gmail_auth send       # cold-mail sender token (token_send.json)
"""

import argparse

from app.config import get_settings
from app.gmail.auth import READONLY_SCOPES, SEND_SCOPES, run_consent_flow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["readonly", "send"])
    args = parser.parse_args()

    settings = get_settings()
    if args.kind == "readonly":
        token, scopes = settings.gmail_readonly_token_file, READONLY_SCOPES
    else:
        token, scopes = settings.gmail_send_token_file, SEND_SCOPES
    token_path = settings.resolve(token)

    run_consent_flow(
        settings.resolve(settings.gmail_credentials_file),
        token_path,
        scopes,
        port=settings.gmail_oauth_port,
    )
    print(f"Saved {args.kind} token to {token_path.name}")


if __name__ == "__main__":
    main()
