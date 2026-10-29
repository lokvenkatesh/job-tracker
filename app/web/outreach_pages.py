"""Cold-mail pages: new campaign form, draft review (edit / regenerate / skip / approve)."""

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import outreach as api
from app.api.deps import get_db, get_settings_dep
from app.config import Settings
from app.db.models import OutreachCampaign, OutreachEmail, OutreachStatus, Resume
from app.outreach import service
from app.outreach.generator import DraftGenerator
from app.outreach.resume import MAX_BYTES, ResumeError
from app.web.routes import _page_context, html_user, is_htmx, render

router = APIRouter(
    prefix="/dashboard/outreach", dependencies=[Depends(html_user)], include_in_schema=False
)


def _form_page(
    request: Request, db: Session, *, error: str | None = None, values=None, status_code=200
):
    resumes = db.scalars(
        select(Resume).order_by(Resume.is_default.desc(), Resume.uploaded_at.desc())
    ).all()
    return render(
        request,
        "outreach.html",
        status_code,
        campaigns=api.list_campaigns(db),
        sent=api.sent_history(db, limit=100),
        resumes=resumes,
        error=error,
        values=values or {},
        **_page_context(db),
    )


@router.get("")
def outreach_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _form_page(request, db)


@router.post("")
async def outreach_create(
    request: Request,
    background: BackgroundTasks,
    jd_text: str = Form(""),
    recipients: str = Form(""),
    recipients_file: UploadFile | None = File(None),
    resume_id: str = Form(""),
    resume_file: UploadFile | None = File(None),
    notes: str = Form(""),
    attach_resume: bool = Form(False),
    override_recent: bool = Form(False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    values = dict(
        jd_text=jd_text,
        recipients=recipients,
        notes=notes,
        attach_resume=attach_resume,
        override_recent=override_recent,
        resume_id=resume_id,
    )
    try:
        generator = api.get_generator(request, settings)
        text = recipients
        if recipients_file is not None and recipients_file.filename:
            raw = await recipients_file.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("The recipient CSV is larger than 1 MB.")
            text = (text + "\n" + raw.decode("utf-8-sig", errors="replace")).strip()
        chosen = int(resume_id) if resume_id.isdigit() else None
        if resume_file is not None and resume_file.filename:
            data = await resume_file.read(MAX_BYTES + 1)
            chosen = service.save_resume(db, resume_file.filename, data, make_default=True).id
        body = api.CampaignCreate(
            jd_text=jd_text or " ",
            recipients=text or " ",
            resume_id=chosen,
            notes=notes or None,
            attach_resume=attach_resume,
            override_recent=override_recent,
        )
        campaign = api.start_campaign(request, body, background, db, settings, generator)
    except (ValueError, ResumeError) as e:
        db.rollback()
        return _form_page(request, db, error=str(e), values=values, status_code=422)
    except HTTPException as e:
        db.rollback()
        return _form_page(
            request, db, error=str(e.detail), values=values, status_code=e.status_code
        )
    rejected = len(campaign.rejected)
    suffix = f"?rejected={rejected}" if rejected else ""
    return RedirectResponse(f"/dashboard/outreach/{campaign.id}{suffix}", 303)


def _campaign_context(db: Session, campaign_id: int, settings: Settings) -> dict:
    campaign = db.get(OutreachCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "Campaign not found")
    emails = [
        api.OutreachEmailOut.model_validate(e)
        for e in db.scalars(
            select(OutreachEmail)
            .where(OutreachEmail.campaign_id == campaign_id)
            .order_by(OutreachEmail.id)
        )
    ]
    counts = {s.value: sum(1 for e in emails if e.status == s) for s in OutreachStatus}
    sent_on = {e.id: e.sent_at for e in emails if e.sent_at}
    return dict(
        campaign=campaign,
        emails=emails,
        counts=counts,
        send=api.send_status(db, campaign, settings),
        followup_of={e.id: sent_on.get(e.followup_of_id) for e in emails if e.followup_of_id},
        pending=sum(1 for e in emails if e.pending),
        ready=sum(
            1
            for e in emails
            if e.status == OutreachStatus.DRAFT and not e.pending and not e.issues and e.body
        ),
    )


@router.get("/{campaign_id}")
def campaign_page(
    request: Request,
    campaign_id: int,
    rejected: int = 0,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    return render(
        request,
        "campaign.html",
        rejected=rejected,
        **_campaign_context(db, campaign_id, settings),
        **_page_context(db),
    )


@router.get("/{campaign_id}/drafts")
def campaign_drafts(
    request: Request,
    campaign_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    return render(request, "_drafts.html", **_campaign_context(db, campaign_id, settings))


def _card(request: Request, db: Session, email: OutreachEmail, error: str | None = None, code=200):
    original = db.get(OutreachEmail, email.followup_of_id) if email.followup_of_id else None
    return render(
        request,
        "_draft.html",
        code,
        e=api.OutreachEmailOut.model_validate(email),
        error=error,
        followup_of={email.id: original.sent_at} if original else {},
    )


@router.post("/emails/{email_id}")
def draft_action(
    request: Request,
    email_id: int,
    action: str = Form("save"),
    subject: str | None = Form(None),
    body: str | None = Form(None),
    attach_resume: bool = Form(False),
    db: Session = Depends(get_db),
) -> Response:
    email = db.get(OutreachEmail, email_id)
    if email is None:
        raise HTTPException(404, "Email not found")
    new_status = {
        "approve": OutreachStatus.APPROVED,
        "unapprove": OutreachStatus.DRAFT,
        "skip": OutreachStatus.SKIPPED,
        "unskip": OutreachStatus.DRAFT,
        "save": None,
    }.get(action, "invalid")
    if new_status == "invalid":
        raise HTTPException(422, "Unknown action")
    try:
        service.update_email(
            db,
            email,
            subject=subject,
            body=body,
            attach_resume=attach_resume if subject is not None else None,
            status=new_status,
        )
    except service.OutreachError as e:
        db.rollback()
        return _card(request, db, db.get(OutreachEmail, email_id), error=str(e), code=409)
    if is_htmx(request):
        return _card(request, db, email)
    return RedirectResponse(f"/dashboard/outreach/{email.campaign_id}", 303)


@router.post("/emails/{email_id}/regenerate")
def draft_regenerate(
    request: Request,
    email_id: int,
    instructions: str = Form(""),
    db: Session = Depends(get_db),
    generator: DraftGenerator = Depends(api.get_generator),
) -> Response:
    email = db.get(OutreachEmail, email_id)
    if email is None:
        raise HTTPException(404, "Email not found")
    try:
        service.regenerate(db, email, generator, instructions.strip()[:1000] or None)
    except service.OutreachError as e:
        return _card(request, db, email, error=str(e), code=409)
    if is_htmx(request):
        return _card(request, db, email)
    return RedirectResponse(f"/dashboard/outreach/{email.campaign_id}", 303)


@router.post("/{campaign_id}/approve-all")
def campaign_approve_all(
    request: Request,
    campaign_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    if db.get(OutreachCampaign, campaign_id) is None:
        raise HTTPException(404, "Campaign not found")
    approved, held = service.approve_all(db, campaign_id)
    db.flush()
    if is_htmx(request):
        return render(
            request,
            "_drafts.html",
            notice=(approved, held),
            **_campaign_context(db, campaign_id, settings),
        )
    return RedirectResponse(f"/dashboard/outreach/{campaign_id}", 303)


@router.post("/{campaign_id}/send")
def campaign_send(
    request: Request,
    campaign_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    error = None
    try:
        service.request_send(db, campaign_id)
    except service.OutreachError as e:
        error = str(e)
    db.flush()
    if is_htmx(request):
        return render(
            request,
            "_drafts.html",
            send_error=error,
            **_campaign_context(db, campaign_id, settings),
        )
    return RedirectResponse(f"/dashboard/outreach/{campaign_id}", 303)


@router.post("/{campaign_id}/pause")
def campaign_pause(
    request: Request,
    campaign_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    try:
        service.pause_send(db, campaign_id)
    except service.OutreachError as e:
        raise HTTPException(404, str(e)) from e
    db.flush()
    if is_htmx(request):
        return render(request, "_drafts.html", **_campaign_context(db, campaign_id, settings))
    return RedirectResponse(f"/dashboard/outreach/{campaign_id}", 303)
