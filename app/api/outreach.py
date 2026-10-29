"""Cold-mail API: resumes, campaigns, drafts. All endpoints require login. Sending is step 8."""

from datetime import datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, computed_field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings_dep, require_user
from app.config import Settings
from app.db.models import OutreachCampaign, OutreachEmail, OutreachStatus, Resume
from app.outreach import service
from app.outreach.checks import word_count
from app.outreach.generator import DraftGenerator
from app.outreach.resume import MAX_BYTES, ResumeError

router = APIRouter(dependencies=[Depends(require_user)], tags=["outreach"])


def get_generator(
    request: Request, settings: Settings = Depends(get_settings_dep)
) -> DraftGenerator:
    generator = getattr(request.app.state, "draft_generator", None)
    if generator is not None:
        return generator
    if settings.anthropic_api_key is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ANTHROPIC_API_KEY is not set")
    request.app.state.draft_generator = DraftGenerator(settings=settings)
    return request.app.state.draft_generator


class ResumeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    uploaded_at: datetime
    is_default: bool
    parsed_text: str = Field(exclude=True)

    @computed_field
    @property
    def characters(self) -> int:
        return len(self.parsed_text)


class OutreachEmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    campaign_id: int
    recipient_email: str
    recipient_name: str | None
    recipient_company: str | None
    recipient_title: str | None
    subject: str
    body: str
    status: OutreachStatus
    skip_reason: str | None
    issues: list[str] | None
    attach_resume: bool
    generated_at: datetime | None
    sent_at: datetime | None
    replied_at: datetime | None
    followup_of_id: int | None
    send_error: str | None

    @computed_field
    @property
    def pending(self) -> bool:
        return self.status == OutreachStatus.DRAFT and self.generated_at is None

    @computed_field
    @property
    def words(self) -> int:
        return word_count(self.body)


class RejectedOut(BaseModel):
    line: int
    text: str
    reason: str


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    company: str | None
    role: str | None
    notes: str | None
    resume_id: int | None
    brief: dict | None
    send_requested_at: datetime | None
    created_at: datetime
    emails: list[OutreachEmailOut] = []
    rejected: list[RejectedOut] = []


class CampaignSummary(BaseModel):
    id: int
    company: str | None
    role: str | None
    created_at: datetime
    counts: dict[str, int]


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jd_text: str = Field(min_length=1, max_length=50_000)
    recipients: str = Field(min_length=1, max_length=50_000, description="paste or CSV text")
    resume_id: int | None = None
    notes: str | None = Field(None, max_length=5_000)
    attach_resume: bool = False
    override_recent: bool = Field(False, description="include people emailed recently")


class EmailPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str | None = Field(None, max_length=300)
    body: str | None = Field(None, max_length=10_000)
    status: OutreachStatus | None = None
    attach_resume: bool | None = None


class RegenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str | None = Field(None, max_length=1_000)


def campaign_out(db: Session, campaign: OutreachCampaign, rejected=()) -> CampaignOut:
    emails = db.scalars(
        select(OutreachEmail)
        .where(OutreachEmail.campaign_id == campaign.id)
        .order_by(OutreachEmail.id)
    ).all()
    return CampaignOut(
        **{
            k: getattr(campaign, k)
            for k in (
                "id",
                "company",
                "role",
                "notes",
                "resume_id",
                "brief",
                "send_requested_at",
                "created_at",
            )
        },
        emails=[OutreachEmailOut.model_validate(e) for e in emails],
        rejected=[RejectedOut(line=r.line, text=r.text, reason=r.reason) for r in rejected],
    )


def _email(db: Session, email_id: int) -> OutreachEmail:
    email = db.get(OutreachEmail, email_id)
    if email is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Email not found")
    return email


def _campaign(db: Session, campaign_id: int) -> OutreachCampaign:
    campaign = db.get(OutreachCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return campaign


@router.post("/resumes", response_model=ResumeOut, status_code=201)
async def upload_resume(
    file: UploadFile = File(...),
    make_default: bool = Form(True),
    db: Session = Depends(get_db),
) -> ResumeOut:
    data = await file.read(MAX_BYTES + 1)
    try:
        resume = service.save_resume(db, file.filename or "resume", data, make_default=make_default)
    except ResumeError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    return ResumeOut.model_validate(resume)


@router.get("/resumes", response_model=list[ResumeOut])
def list_resumes(db: Session = Depends(get_db)) -> list[ResumeOut]:
    rows = db.scalars(select(Resume).order_by(Resume.is_default.desc(), Resume.uploaded_at.desc()))
    return [ResumeOut.model_validate(r) for r in rows]


def start_campaign(
    request: Request,
    body: CampaignCreate,
    background: BackgroundTasks,
    db: Session,
    settings: Settings,
    generator: DraftGenerator,
) -> CampaignOut:
    try:
        campaign, rejected = service.create_campaign(
            db,
            jd_text=body.jd_text,
            recipients_text=body.recipients,
            resume_id=body.resume_id,
            notes=body.notes,
            attach_resume=body.attach_resume,
            override_recent=body.override_recent,
            dedupe_days=settings.outreach_dedupe_days,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    db.commit()  # the background job reads it from its own session
    background.add_task(
        service.generate_pending, request.app.state.session_factory, campaign.id, generator
    )
    return campaign_out(db, campaign, rejected)


@router.post("/outreach/campaigns", response_model=CampaignOut, status_code=201)
def create_campaign(
    request: Request,
    body: CampaignCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    generator: DraftGenerator = Depends(get_generator),
) -> CampaignOut:
    return start_campaign(request, body, background, db, settings, generator)


@router.get("/outreach/campaigns", response_model=list[CampaignSummary])
def list_campaigns(db: Session = Depends(get_db)) -> list[CampaignSummary]:
    counts: dict[int, dict[str, int]] = {}
    for cid, st, n in db.execute(
        select(OutreachEmail.campaign_id, OutreachEmail.status, func.count()).group_by(
            OutreachEmail.campaign_id, OutreachEmail.status
        )
    ):
        counts.setdefault(cid, {})[st] = n
    campaigns = db.scalars(select(OutreachCampaign).order_by(OutreachCampaign.created_at.desc()))
    return [
        CampaignSummary(
            id=c.id,
            company=c.company,
            role=c.role,
            created_at=c.created_at,
            counts=counts.get(c.id, {}),
        )
        for c in campaigns
    ]


@router.get("/outreach/campaigns/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: int, db: Session = Depends(get_db)) -> CampaignOut:
    return campaign_out(db, _campaign(db, campaign_id))


@router.patch("/outreach/emails/{email_id}", response_model=OutreachEmailOut)
def patch_email(
    email_id: int, patch: EmailPatch, db: Session = Depends(get_db)
) -> OutreachEmailOut:
    try:
        email = service.update_email(
            db, _email(db, email_id), **patch.model_dump(exclude_unset=True)
        )
    except service.OutreachError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return OutreachEmailOut.model_validate(email)


@router.post("/outreach/emails/{email_id}/regenerate", response_model=OutreachEmailOut)
def regenerate_email(
    email_id: int,
    body: RegenerateBody | None = None,
    db: Session = Depends(get_db),
    generator: DraftGenerator = Depends(get_generator),
) -> OutreachEmailOut:
    try:
        email = service.regenerate(
            db, _email(db, email_id), generator, body.instructions if body else None
        )
    except service.OutreachError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return OutreachEmailOut.model_validate(email)


@router.post("/outreach/campaigns/{campaign_id}/approve-all")
def approve_all(campaign_id: int, db: Session = Depends(get_db)) -> dict[str, int]:
    _campaign(db, campaign_id)
    approved, held = service.approve_all(db, campaign_id)
    return {"approved": approved, "held_back": held}


class SendStatus(BaseModel):
    campaign_id: int
    sending: bool
    queued: int
    remaining_today: int
    estimated_minutes: int
    gmail_send_connected: bool


def send_status(db: Session, campaign: OutreachCampaign, settings: Settings) -> SendStatus:
    from app.outreach.sender import queue_status
    from app.pipeline.scheduler import send_ready

    q = queue_status(db, campaign.id, settings)
    return SendStatus(
        campaign_id=campaign.id,
        sending=campaign.send_requested_at is not None and q["queued"] > 0,
        gmail_send_connected=send_ready(settings),
        **q,
    )


@router.post("/outreach/campaigns/{campaign_id}/send", response_model=SendStatus)
def send_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> SendStatus:
    """Queue this campaign's approved emails. They go out one at a time, 60-180 s apart, up to
    the daily cap; drafts, skipped and unapproved emails are never sent."""
    try:
        campaign = service.request_send(db, campaign_id)
    except service.OutreachError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return send_status(db, campaign, settings)


@router.post("/outreach/campaigns/{campaign_id}/pause", response_model=SendStatus)
def pause_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> SendStatus:
    try:
        campaign = service.pause_send(db, campaign_id)
    except service.OutreachError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return send_status(db, campaign, settings)


class SentItem(OutreachEmailOut):
    campaign_role: str | None = None
    campaign_company: str | None = None


@router.get("/outreach/sent", response_model=list[SentItem])
def sent_history(db: Session = Depends(get_db), limit: int = 200) -> list[SentItem]:
    rows = db.execute(
        select(OutreachEmail, OutreachCampaign)
        .join(OutreachCampaign, OutreachEmail.campaign_id == OutreachCampaign.id)
        .where(OutreachEmail.status.in_([OutreachStatus.SENT.value, OutreachStatus.REPLIED.value]))
        .order_by(OutreachEmail.sent_at.desc())
        .limit(min(limit, 1000))
    ).all()
    return [
        SentItem(
            **OutreachEmailOut.model_validate(e).model_dump(exclude={"pending", "words"}),
            campaign_role=c.role,
            campaign_company=c.company,
        )
        for e, c in rows
    ]
