"""Applications, review queue, and stats endpoints. All require login."""

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db, get_settings_dep, require_user
from app.api.schemas import (
    ApplicationDetail,
    ApplicationList,
    ApplicationOut,
    ApplicationPatch,
    EmailOut,
    FunnelStage,
    ReviewAction,
    ReviewQueue,
    Stats,
    UpcomingInterview,
    WeekCount,
)
from app.classifier.schema import EmailType
from app.config import Settings
from app.db.models import Application, Email, Interview, Status, StatusEvent, utcnow
from app.db.state_machine import TYPE_TO_STATUS, transition

router = APIRouter(dependencies=[Depends(require_user)], tags=["applications"])

FUNNEL = [Status.APPLIED, Status.ASSESSMENT, Status.INTERVIEW, Status.OFFER]
_RANK = {s: i for i, s in enumerate(FUNNEL)}


def _get_app(db: Session, app_id: int) -> Application:
    app = db.get(Application, app_id)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return app


def _set_status(
    db: Session, app: Application, new: Status, *, email_id: int | None, at: datetime
) -> None:
    if app.status == new:
        return
    db.add(
        StatusEvent(
            application_id=app.id, old_status=app.status, new_status=new.value, email_id=email_id
        )
    )
    app.status = new.value
    app.status_changed_at = at
    app.last_update = max(app.last_update, at)


@router.get("/applications", response_model=ApplicationList)
def list_applications(
    db: Session = Depends(get_db),
    status_: list[Status] | None = Query(None, alias="status"),
    needs_review: bool | None = None,
    q: str | None = Query(None, max_length=100, description="search company or role"),
    sort: str = Query("-last_update", pattern="^-?(last_update|applied_date|company|status)$"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> ApplicationList:
    stmt = select(Application)
    if status_:
        stmt = stmt.where(Application.status.in_([s.value for s in status_]))
    if needs_review is not None:
        stmt = stmt.where(Application.needs_review.is_(needs_review))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(func.lower(Application.company).like(like), func.lower(Application.role).like(like))
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    column = getattr(Application, sort.lstrip("-"))
    order = column.desc().nulls_last() if sort.startswith("-") else column.asc().nulls_last()
    rows = db.scalars(stmt.order_by(order, Application.id.desc()).limit(limit).offset(offset))
    return ApplicationList(total=total, items=[ApplicationOut.model_validate(a) for a in rows])


@router.get("/applications/{app_id}", response_model=ApplicationDetail)
def get_application(app_id: int, db: Session = Depends(get_db)) -> ApplicationDetail:
    app = db.scalar(
        select(Application)
        .where(Application.id == app_id)
        .options(selectinload(Application.emails), selectinload(Application.interviews))
    )
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    events = db.scalars(
        select(StatusEvent)
        .where(StatusEvent.application_id == app_id)
        .order_by(StatusEvent.created_at, StatusEvent.id)
    ).all()
    return ApplicationDetail(
        **ApplicationOut.model_validate(app).model_dump(),
        events=events,
        emails=sorted(app.emails, key=lambda e: e.received_at),
        interviews=sorted(app.interviews, key=lambda i: i.created_at),
    )


@router.patch("/applications/{app_id}", response_model=ApplicationOut)
def update_application(
    app_id: int, patch: ApplicationPatch, db: Session = Depends(get_db)
) -> ApplicationOut:
    app = _get_app(db, app_id)
    fields = patch.model_fields_set
    if "company" in fields:
        if patch.company is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "company cannot be empty")
        app.company = patch.company
    if "role" in fields:
        app.role = patch.role
    if "applied_date" in fields:
        app.applied_date = patch.applied_date
    if "needs_review" in fields and patch.needs_review is not None:
        app.needs_review = patch.needs_review
    if patch.status is not None:
        _set_status(db, app, patch.status, email_id=None, at=utcnow())
    app.last_update = max(app.last_update, utcnow())
    db.flush()
    return ApplicationOut.model_validate(app)


@router.get("/review-queue", response_model=ReviewQueue)
def review_queue(db: Session = Depends(get_db)) -> ReviewQueue:
    emails = db.scalars(
        select(Email).where(Email.needs_review.is_(True)).order_by(Email.received_at.desc())
    ).all()
    apps = db.scalars(
        select(Application)
        .where(Application.needs_review.is_(True))
        .order_by(Application.last_update.desc())
    ).all()
    return ReviewQueue(emails=emails, applications=apps)


@router.post("/review-queue/emails/{email_id}", response_model=EmailOut)
def resolve_review_email(
    email_id: int,
    body: ReviewAction,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> EmailOut:
    email = db.get(Email, email_id)
    if email is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Email not found")
    if not email.needs_review:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email is not in the review queue")

    target = TYPE_TO_STATUS.get(EmailType(email.type)) if email.type in EmailType else None
    if body.action == "link":
        if body.application_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "application_id required")
        app = _get_app(db, body.application_id)
        new = body.status or target
        if new is not None:
            if body.status is not None:  # explicit choice wins, like a manual edit
                _set_status(db, app, new, email_id=email.id, at=email.received_at)
            else:
                t = transition(
                    Status(app.status),
                    new,
                    email_at=email.received_at,
                    status_changed_at=app.status_changed_at,
                )
                if t.new_status is not None:
                    _set_status(db, app, t.new_status, email_id=email.id, at=email.received_at)
        email.application_id = app.id
        app.last_update = max(app.last_update, email.received_at)
    elif body.action == "create":
        company = body.company or email.company
        if not company:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "company required")
        new = body.status or target or Status.APPLIED
        app = Application(
            company=company,
            role=body.role or email.role,
            status=new.value,
            applied_date=(
                email.received_at.astimezone(ZoneInfo(settings.timezone)).date()
                if new is Status.APPLIED
                else None
            ),
            last_update=email.received_at,
            status_changed_at=email.received_at,
            source="review",
            needs_review=False,
        )
        db.add(app)
        db.flush()
        db.add(StatusEvent(application_id=app.id, new_status=new.value, email_id=email.id))
        email.application_id = app.id

    email.needs_review = False
    email.review_reason = None
    db.flush()
    return EmailOut.model_validate(email)


@router.get("/stats", response_model=Stats)
def stats(db: Session = Depends(get_db)) -> Stats:
    apps = db.scalars(select(Application)).all()
    total = len(apps)
    by_status = Counter(Status(a.status) for a in apps)

    reached_rank: dict[int, int] = {a.id: _RANK.get(Status(a.status), 0) for a in apps}
    responded: set[int] = {a.id for a in apps if a.status != Status.APPLIED}
    for app_id, new_status in db.execute(
        select(StatusEvent.application_id, StatusEvent.new_status)
    ):
        s = Status(new_status)
        if s in _RANK:
            reached_rank[app_id] = max(reached_rank.get(app_id, 0), _RANK[s])
        if s is not Status.APPLIED:
            responded.add(app_id)
    funnel = [
        FunnelStage(stage=s, reached=sum(1 for r in reached_rank.values() if r >= _RANK[s]))
        for s in FUNNEL
    ]

    weeks: defaultdict[datetime, int] = defaultdict(int)
    for a in apps:
        d = a.applied_date or a.created_at.date()
        weeks[d - timedelta(days=d.weekday())] += 1
    this_week = datetime.now(UTC).date()
    this_week -= timedelta(days=this_week.weekday())
    weekly = [
        WeekCount(week_start=w, applications=weeks.get(w, 0))
        for w in (this_week - timedelta(weeks=i) for i in range(11, -1, -1))
    ]

    now = utcnow()
    upcoming = db.execute(
        select(Interview, Application)
        .join(Application, Interview.application_id == Application.id)
        .where(or_(Interview.scheduled_at.is_(None), Interview.scheduled_at >= now))
        .where(Application.status != Status.REJECTED.value)
        .order_by(Interview.scheduled_at.asc().nulls_last())
    ).all()

    return Stats(
        total_applications=total,
        by_status={s: by_status.get(s, 0) for s in Status},
        funnel=funnel,
        response_rate=round(len(responded) / total, 3) if total else 0.0,
        interview_rate=round(funnel[2].reached / total, 3) if total else 0.0,
        needs_review=db.scalar(select(func.count()).where(Email.needs_review.is_(True)))
        + sum(1 for a in apps if a.needs_review),
        recruiter_outreach=db.scalar(
            select(func.count()).where(Email.type == EmailType.RECRUITER_OUTREACH.value)
        ),
        weekly=weekly,
        upcoming_interviews=[
            UpcomingInterview(
                application_id=a.id,
                company=a.company,
                role=a.role,
                scheduled_at=i.scheduled_at,
                meeting_link=i.meeting_link,
            )
            for i, a in upcoming
        ],
    )
