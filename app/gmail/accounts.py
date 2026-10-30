"""Connected Gmail inboxes: encrypted token storage, scope rules, Gmail services per inbox, the
in-app "Connect Gmail" OAuth flow, and the one-time import of the original token files.

Each inbox holds two independent tokens. "read" grants gmail.readonly (tracker); "send" grants
gmail.send (cold mail). A token is never accepted with the other Gmail scope, so the tracker can't
send and the sender can't read. Both also carry openid + email, used only to confirm which Google
account was connected.
"""

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Email, MailAccount, OutreachCampaign, OutreachEmail, SyncStateRow
from app.db.session import session_scope
from app.gmail.auth import READONLY_SCOPES, SEND_SCOPES, GmailAuthError, build_gmail_service

log = logging.getLogger(__name__)

Kind = Literal["read", "send"]
IDENTITY_SCOPES = ("openid", "https://www.googleapis.com/auth/userinfo.email")
GMAIL_SCOPE = {"read": READONLY_SCOPES[0], "send": SEND_SCOPES[0]}
CALLBACK_PATH = "/oauth/google/callback"


class AccountError(RuntimeError):
    pass


# --- encryption ------------------------------------------------------------------


def _fernet(settings: Settings) -> Fernet:
    if settings.token_encryption_key is None:
        raise AccountError("TOKEN_ENCRYPTION_KEY is not set")
    try:
        return Fernet(settings.token_encryption_key.get_secret_value().encode())
    except ValueError as e:
        raise AccountError("TOKEN_ENCRYPTION_KEY is not a valid key") from e


def encrypt(settings: Settings, plaintext: str) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt(settings: Settings, ciphertext: str) -> str:
    try:
        return _fernet(settings).decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise AccountError("Stored token can't be decrypted (TOKEN_ENCRYPTION_KEY changed?)") from e


def new_encryption_key() -> str:
    return Fernet.generate_key().decode()


# --- scopes ----------------------------------------------------------------------


def scopes_for(kind: Kind) -> list[str]:
    return [GMAIL_SCOPE[kind], *IDENTITY_SCOPES]


def check_token_scopes(granted: Iterable[str] | None, kind: Kind) -> None:
    granted = set(granted or ())
    other = GMAIL_SCOPE["send" if kind == "read" else "read"]
    if GMAIL_SCOPE[kind] not in granted:
        raise AccountError(f"Google didn't grant {GMAIL_SCOPE[kind].rsplit('/', 1)[-1]}")
    if other in granted:
        raise AccountError("A token may hold only one Gmail permission")
    extra = granted - {GMAIL_SCOPE[kind], *IDENTITY_SCOPES, "email"}
    if extra:
        raise AccountError(f"Unexpected permissions granted: {sorted(extra)}")


# --- credentials & services per inbox --------------------------------------------


def _token_column(kind: Kind) -> str:
    return "readonly_token" if kind == "read" else "send_token"


def credentials_for(
    account: MailAccount, kind: Kind, settings: Settings, session_factory: sessionmaker[Session]
) -> Credentials:
    stored = getattr(account, _token_column(kind))
    if not stored:
        raise AccountError(f"{account.email} has no {kind} permission connected")
    try:
        creds = Credentials.from_authorized_user_info(json.loads(decrypt(settings, stored)))
    except ValueError as e:  # malformed, or saved without a refresh token
        raise AccountError(f"{account.email} needs reconnecting") from e
    check_token_scopes(creds.scopes, kind)
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise AccountError(f"{account.email} needs reconnecting")
        try:
            creds.refresh(Request())
        except Exception as e:  # google.auth.exceptions.RefreshError and transport errors
            raise AccountError(f"{account.email} needs reconnecting ({type(e).__name__})") from e
        with session_scope(session_factory) as db:  # persist the new access token
            db.execute(
                update(MailAccount)
                .where(MailAccount.id == account.id)
                .values({_token_column(kind): encrypt(settings, creds.to_json())})
            )
    return creds


def gmail_service(
    account: MailAccount, kind: Kind, settings: Settings, session_factory: sessionmaker[Session]
):
    return build_gmail_service(credentials_for(account, kind, settings, session_factory))


def default_sender(db: Session) -> MailAccount | None:
    return db.scalars(
        select(MailAccount)
        .where(MailAccount.send_token.is_not(None), MailAccount.enabled.is_(True))
        .order_by(MailAccount.id)
    ).first()


# --- in-app OAuth ("Connect Gmail") ----------------------------------------------


@dataclass(frozen=True)
class OAuthStart:
    url: str
    state: str
    code_verifier: str


def _client_config(settings: Settings) -> dict:
    if settings.google_oauth_client_json is not None:
        try:
            return json.loads(settings.google_oauth_client_json.get_secret_value())
        except json.JSONDecodeError as e:
            raise AccountError("GOOGLE_OAUTH_CLIENT_JSON isn't valid JSON") from e
    path = settings.resolve(settings.gmail_credentials_file)
    if not path.exists():
        raise AccountError(f"{path.name} is missing (Google OAuth client file)")
    return json.loads(path.read_text())


def _flow(settings: Settings, kind: Kind, state: str | None = None) -> Flow:
    if settings.public_base_url.startswith(("http://localhost", "http://127.0.0.1")):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # local http callback only
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")  # we check scopes ourselves
    return Flow.from_client_config(
        _client_config(settings),
        scopes=scopes_for(kind),
        redirect_uri=settings.public_base_url.rstrip("/") + CALLBACK_PATH,
        state=state,
    )


def start_oauth(settings: Settings, kind: Kind, login_hint: str | None = None) -> OAuthStart:
    flow = _flow(settings, kind)
    params = {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "false"}
    if login_hint:
        params["login_hint"] = login_hint
    url, state = flow.authorization_url(**params)
    return OAuthStart(url=url, state=state, code_verifier=flow.code_verifier)


@dataclass(frozen=True)
class OAuthResult:
    email: str
    token_json: str


def finish_oauth(
    settings: Settings, kind: Kind, *, state: str, code_verifier: str, authorization_response: str
) -> OAuthResult:
    flow = _flow(settings, kind, state=state)
    flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=authorization_response)
    creds = flow.credentials
    check_token_scopes(creds.scopes, kind)
    if not creds.refresh_token:
        raise AccountError("Google didn't return a long-lived token; please try again")
    claims = google_id_token.verify_oauth2_token(creds.id_token, Request(), creds.client_id)
    if not claims.get("email") or not claims.get("email_verified", False):
        raise AccountError("Couldn't confirm which Google account was connected")
    return OAuthResult(email=claims["email"].lower(), token_json=creds.to_json())


def save_connection(
    db: Session,
    settings: Settings,
    result: OAuthResult,
    kind: Kind,
    *,
    account_id: int | None = None,
    label: str | None = None,
    backfill_days: int = 30,
) -> MailAccount:
    """Store a fresh token. A read token creates the inbox (or reconnects it); a send token
    must belong to the same Google account as the inbox it's added to."""
    account = db.get(MailAccount, account_id) if account_id else None
    existing = db.scalars(select(MailAccount).where(MailAccount.email == result.email)).first()
    if account is not None and account.email != result.email:
        raise AccountError(
            f"You signed in as {result.email}, but this inbox is {account.email}. "
            "Choose the matching Google account."
        )
    account = account or existing
    if account is None:
        if kind == "send":
            raise AccountError("Connect the inbox for reading first, then allow sending.")
        account = MailAccount(
            email=result.email,
            label=(label or result.email.split("@")[0])[:100],
            initial_backfill_days=backfill_days,
            enabled=True,
        )
        db.add(account)
    setattr(account, _token_column(kind), encrypt(settings, result.token_json))
    account.last_error = None
    db.flush()
    return account


def disconnect(db: Session, account: MailAccount, kind: Kind | Literal["all"]) -> None:
    """Forget stored tokens. Data already tracked from this inbox is kept."""
    if kind in ("read", "all"):
        account.readonly_token = None
        account.enabled = False
    if kind in ("send", "all"):
        account.send_token = None
    db.flush()


# --- one-time import of the original token files ----------------------------------


def ensure_encryption_key(settings: Settings) -> Settings:
    """Locally, create TOKEN_ENCRYPTION_KEY in .env the first time it's needed."""
    if settings.token_encryption_key is not None:
        return settings
    from app.config import PROJECT_ROOT
    from scripts.set_secret import set_value

    env = PROJECT_ROOT / ".env"
    if not env.exists():
        raise AccountError("TOKEN_ENCRYPTION_KEY is not set")
    key = new_encryption_key()
    env.write_text(set_value(env.read_text(), "TOKEN_ENCRYPTION_KEY", key))
    os.chmod(env, 0o600)
    os.environ["TOKEN_ENCRYPTION_KEY"] = key  # visible to settings loaded later in this process
    from app.config import get_settings

    get_settings.cache_clear()
    log.info("Created TOKEN_ENCRYPTION_KEY in .env")
    return settings.model_copy(update={"token_encryption_key": SecretStr(key)})


def _file_token_with_identity(path, scopes: tuple[str, ...]) -> str | None:
    if not path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(path))
    try:
        from app.gmail.auth import check_scopes

        check_scopes(creds.scopes, scopes)
    except GmailAuthError:
        return None
    return creds.to_json()


def import_legacy_tokens(settings: Settings, session_factory: sessionmaker[Session]) -> Settings:
    """If no inbox exists yet but token.json does (set up by scripts.gmail_auth), turn it into
    inbox #1 and point all existing data at it. Idempotent. Returns settings (with the
    encryption key, if one had to be created)."""
    with session_scope(session_factory) as db:
        if db.scalars(select(MailAccount)).first() is not None:
            return settings
    read_json = _file_token_with_identity(
        settings.resolve(settings.gmail_readonly_token_file), READONLY_SCOPES
    )
    if read_json is None:
        return settings
    settings = ensure_encryption_key(settings)
    service = build_gmail_service(Credentials.from_authorized_user_info(json.loads(read_json)))
    email = service.users().getProfile(userId="me").execute()["emailAddress"].lower()
    send_json = _file_token_with_identity(
        settings.resolve(settings.gmail_send_token_file), SEND_SCOPES
    )
    with session_scope(session_factory) as db:
        state = db.get(SyncStateRow, 1)
        account = MailAccount(
            email=email,
            label="Primary",
            readonly_token=encrypt(settings, read_json),
            send_token=encrypt(settings, send_json) if send_json else None,
            enabled=True,
            last_history_id=state.last_history_id if state else None,
            last_run_at=state.last_run_at if state else None,
        )
        db.add(account)
        db.flush()
        for model in (Email, OutreachCampaign, OutreachEmail):
            db.execute(
                update(model)
                .where(model.mail_account_id.is_(None))
                .values(mail_account_id=account.id)
            )
        log.info("Imported %s as inbox #%d", email, account.id)
    return settings
