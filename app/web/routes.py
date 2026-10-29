"""Server-rendered dashboard (Jinja2 + HTMX). Pages live under /dashboard; the JSON API keeps the
spec's paths. Page handlers reuse the API functions so both stay in sync."""

import hashlib
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import applications as api
from app.api.auth import authenticate
from app.api.deps import SESSION_USER_KEY, get_db, get_settings_dep
from app.api.schemas import ApplicationPatch, ReviewAction
from app.config import Settings
from app.db.models import Application, Email, Status, StatusEvent
from app.web.charts import weekly_bars

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")


@lru_cache
def _asset_version(path: str) -> str:
    return hashlib.sha256((WEB_DIR / "static" / path).read_bytes()).hexdigest()[:10]


def asset(path: str) -> str:
    """Static URL with a content hash, so browsers fetch new CSS/JS after an update."""
    return f"/static/{path}?v={_asset_version(path)}"


templates.env.globals["asset"] = asset

STATUS_ORDER = [s for s in Status]
STATUS_LABELS = {s: s.value.capitalize() for s in Status}


class LoginRequired(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


def html_user(request: Request, settings: Settings = Depends(get_settings_dep)) -> str:
    user = request.session.get(SESSION_USER_KEY)
    if user is None or user != settings.dashboard_username:
        raise LoginRequired(request.url.path)
    return user


def safe_next(path: str | None) -> str:
    """Only same-site dashboard paths; never '//evil.com' or absolute URLs."""
    if path and path.startswith("/dashboard") and not path.startswith("//"):
        return path
    return "/dashboard"


def _localtime(value: datetime | None, fmt: str, tz: str) -> str:
    if value is None:
        return ""
    local = value.astimezone(ZoneInfo(tz))
    return local.strftime(fmt).replace(" 0", " ").lstrip("0")


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    context.setdefault("user", request.session.get(SESSION_USER_KEY))
    return templates.TemplateResponse(
        request,
        name,
        {
            "statuses": STATUS_ORDER,
            "status_labels": STATUS_LABELS,
            "tz": settings.timezone,
            "local": lambda v, fmt="%b %d, %Y %I:%M %p": _localtime(v, fmt, settings.timezone),
            "nav": request.url.path,
            **context,
        },
        status_code=status_code,
    )


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


# --- public: login/logout ----------------------------------------------------

public = APIRouter(include_in_schema=False)


@public.api_route("/", methods=["GET", "HEAD"])
def root() -> RedirectResponse:
    return RedirectResponse("/dashboard", status.HTTP_303_SEE_OTHER)


@public.get("/login")
def login_page(request: Request, next: str | None = None) -> HTMLResponse:
    return render(request, "login.html", next=safe_next(next), error=None, user=None)


@public.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/dashboard"),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    try:
        authenticate(request, username, password, settings)
    except HTTPException as e:
        return render(
            request, "login.html", e.status_code, next=safe_next(next), error=e.detail, user=None
        )
    return RedirectResponse(safe_next(next), status.HTTP_303_SEE_OTHER)


@public.post("/logout")
def logout_submit(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)


# --- dashboard ---------------------------------------------------------------

router = APIRouter(prefix="/dashboard", dependencies=[Depends(html_user)], include_in_schema=False)


def _review_count(db: Session) -> int:
    emails = db.scalar(select(func.count()).where(Email.needs_review.is_(True))) or 0
    apps = db.scalar(select(func.count()).where(Application.needs_review.is_(True))) or 0
    return emails + apps


def _summary(db: Session) -> dict:
    """Side panel: how many are still in play vs rejected, and every rejection."""
    counts = dict(
        db.execute(select(Application.status, func.count()).group_by(Application.status)).all()
    )
    by_status = {s: counts.get(s.value, 0) for s in Status}
    rejected = db.scalars(
        select(Application)
        .where(Application.status == Status.REJECTED.value)
        .order_by(Application.status_changed_at.desc(), Application.id.desc())
    ).all()
    total = sum(by_status.values())
    return {
        "total": total,
        "active": total - by_status[Status.REJECTED],
        "waiting": by_status[Status.APPLIED],
        "in_process": by_status[Status.ASSESSMENT]
        + by_status[Status.INTERVIEW]
        + by_status[Status.OFFER],
        "rejected_count": by_status[Status.REJECTED],
        "rejected": rejected,
    }


def _page_context(db: Session) -> dict:
    return {"review_count": _review_count(db), "summary": _summary(db)}


def _recent_activity(db: Session, limit: int = 12) -> list[dict]:
    when = func.coalesce(Email.received_at, StatusEvent.created_at)
    rows = db.execute(
        select(StatusEvent, Application, Email, when.label("at"))
        .join(Application, StatusEvent.application_id == Application.id)
        .outerjoin(Email, StatusEvent.email_id == Email.id)
        .order_by(when.desc(), StatusEvent.id.desc())
        .limit(limit)
    ).all()
    return [
        {"event": ev, "app": app, "email": email, "at": at, "manual": ev.email_id is None}
        for ev, app, email, at in rows
    ]


@router.get("")
def overview(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    stats = api.stats(db)
    chart = weekly_bars([(w.week_start, w.applications) for w in stats.weekly])
    return render(
        request,
        "overview.html",
        stats=stats,
        chart=chart,
        activity=_recent_activity(db),
        **_page_context(db),
    )


def _list(db: Session, status_: list[Status] | None, q: str | None, sort: str, review: bool):
    return api.list_applications(
        db=db,
        status_=status_ or None,
        needs_review=True if review else None,
        q=q or None,
        sort=sort,
        limit=1000,
        offset=0,
    )


@router.get("/applications")
def applications_page(
    request: Request,
    db: Session = Depends(get_db),
    status_: list[Status] = Query([], alias="status"),
    q: str = Query("", max_length=100),
    sort: str = Query("-last_update", pattern="^-?(last_update|applied_date|company|status)$"),
    review: bool = False,
) -> HTMLResponse:
    result = _list(db, status_, q, sort, review)
    context = dict(
        result=result,
        selected=set(status_),
        q=q,
        sort=sort,
        review=review,
        **_page_context(db),
    )
    if is_htmx(request):
        return render(request, "_table.html", **context)
    return render(request, "applications.html", **context)


@router.post("/applications/{app_id}/status")
def change_status(
    request: Request,
    app_id: int,
    new_status: Status = Form(..., alias="status"),
    db: Session = Depends(get_db),
) -> Response:
    app = api.update_application(app_id, ApplicationPatch(status=new_status), db)
    if is_htmx(request):
        return render(request, "_row.html", app=app)
    return RedirectResponse(request.headers.get("referer") or "/dashboard/applications", 303)


@router.get("/board")
def board(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    apps = db.scalars(select(Application).order_by(Application.last_update.desc())).all()
    columns = {s: [a for a in apps if a.status == s.value] for s in STATUS_ORDER}
    return render(request, "board.html", columns=columns, **_page_context(db))


@router.get("/applications/{app_id}")
def application_page(
    request: Request, app_id: int, saved: bool = False, db: Session = Depends(get_db)
) -> HTMLResponse:
    detail = api.get_application(app_id, db)
    emails = {e.id: e for e in detail.emails}
    timeline = [
        {
            "kind": "event",
            "at": emails[e.email_id].received_at if e.email_id in emails else e.created_at,
            "event": e,
            "email": emails.get(e.email_id),
        }
        for e in detail.events
    ]
    linked = {e.email_id for e in detail.events}
    timeline += [
        {"kind": "email", "at": e.received_at, "email": e}
        for e in detail.emails
        if e.id not in linked
    ]
    timeline.sort(key=lambda item: item["at"])
    return render(
        request,
        "application.html",
        app=detail,
        timeline=timeline,
        saved=saved,
        **_page_context(db),
    )


@router.post("/applications/{app_id}")
def application_save(
    app_id: int,
    company: str = Form(...),
    role: str = Form(""),
    applied_date: str = Form(""),
    status_: Status = Form(..., alias="status"),
    needs_review: bool = Form(False),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        parsed_date = date.fromisoformat(applied_date) if applied_date else None
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date") from None
    api.update_application(
        app_id,
        ApplicationPatch(
            company=company.strip() or None,
            role=role.strip() or None,
            applied_date=parsed_date,
            status=status_,
            needs_review=needs_review,
        ),
        db,
    )
    return RedirectResponse(f"/dashboard/applications/{app_id}?saved=1", 303)


@router.get("/review")
def review_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    queue = api.review_queue(db)
    apps = db.scalars(select(Application).order_by(func.lower(Application.company))).all()
    return render(request, "review.html", queue=queue, apps=apps, **_page_context(db))


@router.post("/review/emails/{email_id}")
def review_resolve(
    request: Request,
    email_id: int,
    action: str = Form(...),
    application_id: int | None = Form(None),
    company: str = Form(""),
    role: str = Form(""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    try:
        body = ReviewAction(
            action=action,
            application_id=application_id,
            company=company.strip() or None,
            role=role.strip() or None,
        )
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid review action") from e
    api.resolve_review_email(email_id, body, db, settings)
    if is_htmx(request):
        db.flush()
        return render(request, "_review_done.html", review_count=_review_count(db))
    return RedirectResponse("/dashboard/review", 303)


@router.post("/review/applications/{app_id}")
def review_clear_app(request: Request, app_id: int, db: Session = Depends(get_db)) -> Response:
    api.update_application(app_id, ApplicationPatch(needs_review=False), db)
    if is_htmx(request):
        db.flush()
        return render(request, "_review_done.html", review_count=_review_count(db))
    return RedirectResponse("/dashboard/review", 303)


def login_redirect(request: Request, exc: LoginRequired) -> Response:
    target = f"/login?next={quote(safe_next(exc.next_path))}"
    if is_htmx(request):  # full-page redirect instead of swapping the login page into a fragment
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status.HTTP_303_SEE_OTHER)
