"""Gmail OAuth. The tracker and the cold-mail sender use separate tokens with exactly one
scope each, so the tracker can never send, modify, or delete mail."""

import logging
import os
from collections.abc import Iterable
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

READONLY_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
SEND_SCOPES = ("https://www.googleapis.com/auth/gmail.send",)


class GmailAuthError(RuntimeError):
    pass


def check_scopes(granted: Iterable[str] | None, required: Iterable[str]) -> None:
    """Require the token's scopes to match exactly: no missing scopes and no extra ones."""
    granted_set, required_set = set(granted or ()), set(required)
    if granted_set != required_set:
        raise GmailAuthError(
            f"Token scopes {sorted(granted_set)} do not match required {sorted(required_set)}. "
            "Delete the token file and re-run the auth script."
        )


def _write_token(token_path: Path, creds: Credentials) -> None:
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600)


def load_credentials(token_path: Path, scopes: Iterable[str]) -> Credentials:
    scopes = tuple(scopes)
    if not token_path.exists():
        kind = "send" if scopes == SEND_SCOPES else "readonly"
        raise GmailAuthError(
            f"{token_path.name} not found. Run: python -m scripts.gmail_auth {kind}"
        )
    creds = Credentials.from_authorized_user_file(str(token_path))
    check_scopes(creds.scopes, scopes)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _write_token(token_path, creds)
            log.info("Refreshed Gmail token %s", token_path.name)
        else:
            raise GmailAuthError(f"{token_path.name} is invalid. Re-run the auth script.")
    return creds


def run_consent_flow(
    credentials_path: Path, token_path: Path, scopes: Iterable[str], *, port: int = 8080
) -> Credentials:
    """Open the browser for Google consent and store the resulting token.

    Works with both "Web application" and "Desktop app" OAuth clients. A Web client must list
    http://localhost:{port}/ as an authorized redirect URI, so the port is fixed, not random."""
    if not credentials_path.exists():
        raise GmailAuthError(
            f"{credentials_path.name} not found. Download the OAuth Desktop client JSON from "
            "Google Cloud Console and save it there (see README, 'Gmail API setup')."
        )
    scopes = tuple(scopes)
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), list(scopes))
    creds = flow.run_local_server(port=port, open_browser=True)
    check_scopes(creds.scopes, scopes)
    _write_token(token_path, creds)
    return creds


def build_gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_readonly_service(settings: Settings | None = None):
    settings = settings or get_settings()
    token = settings.resolve(settings.gmail_readonly_token_file)
    return build_gmail_service(load_credentials(token, READONLY_SCOPES))


def get_send_service(settings: Settings | None = None):
    settings = settings or get_settings()
    token = settings.resolve(settings.gmail_send_token_file)
    return build_gmail_service(load_credentials(token, SEND_SCOPES))
