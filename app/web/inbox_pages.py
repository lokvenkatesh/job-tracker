"""Inboxes page: connect more Gmail inboxes, see each one's details, reconnect, pause, remove."""

import logging
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings_dep
from app.config import Settings
from app.db.models import Email, MailAccount, OutreachEmail, OutreachStatus
from app.gmail import accounts
from app.web.routes import _page_context, html_user, render

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(html_user)], include_in_schema=False)
OAUTH_SESSION_KEY = "oauth"


def _scan_in_background(background: BackgroundTasks, account_id: int) -> None:
    from app.pipeline.scheduler import run_tracker_job

    background.add_task(run_tracker_job, None, [account_id])


def _inbox_rows(db: Session) -> list[dict]:
    rows = []
    for a in db.scalars(select(MailAccount).order_by(MailAccount.id)):
        processed = db.scalar(select(func.count()).where(Email.mail_account_id == a.id)) or 0
        apps = (
            db.scalar(
                select(func.count(distinct(Email.application_id))).where(
                    Email.mail_account_id == a.id, Email.application_id.is_not(None)
                )
            )
            or 0
        )
        sent = (
            db.scalar(
                select(func.count()).where(
                    OutreachEmail.mail_account_id == a.id,
                    OutreachEmail.status.in_(
                        [OutreachStatus.SENT.value, OutreachStatus.REPLIED.value]
                    ),
                )
            )
            or 0
        )
        if not a.readonly_token:
            state = "disconnected"
        elif not a.enabled:
            state = "paused"
        elif a.last_error:
            state = "needs attention"
        elif a.last_run_at is None:
            state = "first scan pending"
        else:
            state = "active"
        rows.append(dict(a=a, processed=processed, apps=apps, sent=sent, state=state))
    return rows


@router.get("/dashboard/inboxes")
def inboxes_page(
    request: Request,
    connected: str | None = None,
    error: str | None = None,
    notice: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    return render(
        request,
        "inboxes.html",
        inboxes=_inbox_rows(db),
        connected=connected,
        error=error,
        notice=notice,
        callback_url=settings.public_base_url.rstrip("/") + accounts.CALLBACK_PATH,
        **_page_context(db),
    )


def _back(error: str | None = None, **params) -> RedirectResponse:
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v is not None)
    if error:
        query = (query + "&" if query else "") + f"error={quote(error)}"
    return RedirectResponse("/dashboard/inboxes" + (f"?{query}" if query else ""), 303)


@router.post("/dashboard/inboxes/connect")
def connect_start(
    request: Request,
    kind: str = Form("read"),
    account_id: int | None = Form(None),
    label: str = Form(""),
    backfill_days: int = Form(30),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> RedirectResponse:
    if kind not in ("read", "send"):
        raise HTTPException(422, "Unknown connection type")
    account = db.get(MailAccount, account_id) if account_id else None
    if account_id and account is None:
        raise HTTPException(404, "Inbox not found")
    try:
        settings = accounts.ensure_encryption_key(settings)
        start = accounts.start_oauth(settings, kind, login_hint=account.email if account else None)
    except accounts.AccountError as e:
        return _back(str(e))
    request.session[OAUTH_SESSION_KEY] = {
        "state": start.state,
        "verifier": start.code_verifier,
        "kind": kind,
        "account_id": account.id if account else None,
        "label": label.strip()[:100] or None,
        "backfill_days": max(0, min(backfill_days, 365)),
    }
    return RedirectResponse(start.url, 303)


@router.get(accounts.CALLBACK_PATH)
def connect_finish(
    request: Request,
    background: BackgroundTasks,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> RedirectResponse:
    pending = request.session.pop(OAUTH_SESSION_KEY, None)
    if error:
        return _back("Google sign-in was cancelled." if error == "access_denied" else error)
    if not pending or not state or state != pending.get("state"):
        return _back("That sign-in link expired or didn't come from this page. Try again.")
    # Rebuild the URL Google redirected to from the configured public address (behind a proxy
    # the app may see http:// internally).
    authorization_response = (
        settings.public_base_url.rstrip("/") + accounts.CALLBACK_PATH + "?" + request.url.query
    )
    kind = pending["kind"]
    try:
        settings = accounts.ensure_encryption_key(settings)
        result = accounts.finish_oauth(
            settings,
            kind,
            state=state,
            code_verifier=pending["verifier"],
            authorization_response=authorization_response,
        )
        is_new = (
            db.scalars(select(MailAccount).where(MailAccount.email == result.email)).first() is None
        )
        account = accounts.save_connection(
            db,
            settings,
            result,
            kind,
            account_id=pending.get("account_id"),
            label=pending.get("label"),
            backfill_days=pending.get("backfill_days", 30),
        )
        if kind == "read":
            account.enabled = True
    except accounts.AccountError as e:
        db.rollback()
        return _back(str(e))
    except Exception as e:  # oauthlib / network errors: show a short reason, log the rest
        db.rollback()
        log.warning("Gmail connection failed: %s", type(e).__name__)
        return _back(f"Google sign-in failed ({type(e).__name__}). Try again.")
    db.commit()
    if kind == "read" and is_new:
        _scan_in_background(background, account.id)
    return _back(connected=f"{account.email} ({'reading' if kind == 'read' else 'sending'})")


def _account(db: Session, account_id: int) -> MailAccount:
    account = db.get(MailAccount, account_id)
    if account is None:
        raise HTTPException(404, "Inbox not found")
    return account


@router.post("/dashboard/inboxes/{account_id}/rename")
def rename(account_id: int, label: str = Form(...), db: Session = Depends(get_db)):
    label = label.strip()[:100]
    if not label:
        return _back("Give the inbox a name.")
    _account(db, account_id).label = label
    return _back(notice="Renamed.")


@router.post("/dashboard/inboxes/{account_id}/pause")
def pause(account_id: int, db: Session = Depends(get_db)):
    _account(db, account_id).enabled = False
    return _back(notice="Paused: this inbox won't be scanned until you resume it.")


@router.post("/dashboard/inboxes/{account_id}/resume")
def resume(account_id: int, db: Session = Depends(get_db)):
    account = _account(db, account_id)
    if not account.readonly_token:
        return _back("Reconnect this inbox first.")
    account.enabled = True
    return _back(notice="Resumed.")


@router.post("/dashboard/inboxes/{account_id}/sync")
def sync_now(account_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    account = _account(db, account_id)
    if not account.readonly_token or not account.enabled:
        return _back("This inbox is paused or disconnected.")
    _scan_in_background(background, account.id)
    return _back(notice=f"Scanning {account.email} now; refresh in a minute.")


@router.post("/dashboard/inboxes/{account_id}/disconnect")
def disconnect(account_id: int, what: str = Form("all"), db: Session = Depends(get_db)):
    if what not in ("send", "all"):
        raise HTTPException(422, "Unknown option")
    account = _account(db, account_id)
    accounts.disconnect(db, account, what)
    message = (
        "Sending turned off for this inbox."
        if what == "send"
        else "Disconnected. Its applications and history stay on the dashboard."
    )
    return _back(notice=message)
