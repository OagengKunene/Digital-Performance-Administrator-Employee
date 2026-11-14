import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import date
from typing import Literal
from fastapi import FastAPI, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, text, func
from sqlalchemy.exc import IntegrityError
from .db import get_db, User, LoginSession, SchoolClass, Learner, Mark, Intervention, Audit, AgentRun, AgentJob, MonitoringPolicy, FollowUp as MonitorFollowUp, WorkerHeartbeat, utcnow
from .security import current_user, verify_password, hash_password, token_hash
from .services import allowed_class, audit, evidence, import_csv, wake_monitor

app = FastAPI(title="Performance Administrator", version="0.3.0")
attempts = defaultdict(deque)


def settings_provider():
    from .config import settings
    return settings().ai_provider


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(StrictBody):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)


class Upload(StrictBody):
    csv: str = Field(max_length=2_000_000)


class Correction(StrictBody):
    earned: float | None = Field(allow_inf_nan=False)
    reason: str = Field(min_length=10, max_length=1000)


class Support(StrictBody):
    learner_id: str = Field(max_length=80)
    topic: str = Field(min_length=1, max_length=120)
    activity: str = Field(min_length=5, max_length=2000)
    due: date


class FollowUp(StrictBody):
    status: Literal["assigned", "in_progress", "completed", "requires_review"]
    follow_up: str = Field(max_length=2000)


class Goal(StrictBody):
    goal: str = Field(min_length=10, max_length=2000)
    # None = the installation's AI_PROVIDER. "offline" needs no key.
    provider: Literal["gemini", "groq", "offline"] | None = None
    # Offline only: first report draft omits coverage so the reviewer's repair loop is visible.
    demonstrate_repair: bool = False


class ResumeRun(StrictBody):
    provider: Literal["gemini", "groq", "offline"] | None = None


class PolicyBody(StrictBody):
    enabled: bool = False
    use_ai: bool = False
    on_record_change: bool = True
    interval_minutes: int = Field(default=1440, ge=15, le=10080)
    daily_ai_limit: int = Field(default=1, ge=1, le=5)


class FollowUpOutcome(StrictBody):
    status: Literal["acknowledged", "resolved"]
    outcome: str = Field(min_length=10, max_length=2000)


class NewStaff(Login):
    password: str = Field(min_length=12, max_length=1024)
    role: Literal["teacher", "principal"]


class NewClass(StrictBody):
    name: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=120)
    teacher_id: int
    support_threshold: float = Field(default=50, ge=0, le=100, allow_inf_nan=False)


def principal(user):
    if user.role != "principal":
        raise HTTPException(403, "Principal access required")


@app.get("/staff")
def staff(user=Depends(current_user), db=Depends(get_db)):
    principal(user)
    return [{"id": u.id, "email": u.email, "role": u.role} for u in db.scalars(select(User).where(User.school == user.school))]


@app.post("/staff")
def create_staff(body: NewStaff, user=Depends(current_user), db=Depends(get_db)):
    principal(user)
    email = body.email.strip().lower()
    if "@" not in email or db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(422, "Invalid or unavailable email")
    item = User(email=email, password_hash=hash_password(body.password), role=body.role, school=user.school)
    db.add(item)
    db.commit()
    return {"id": item.id, "email": item.email}


@app.post("/classes")
def create_class(body: NewClass, user=Depends(current_user), db=Depends(get_db)):
    principal(user)
    teacher = db.get(User, body.teacher_id)
    if not teacher or teacher.school != user.school or teacher.role != "teacher":
        raise HTTPException(422, "Select a teacher from this school")
    item = SchoolClass(school=user.school, **body.model_dump())
    db.add(item)
    db.commit()
    return {"id": item.id}


@app.post("/classes/{class_id}/learners")
def import_roster(class_id: int, body: Upload, user=Depends(current_user), db=Depends(get_db)):
    import csv
    import io
    principal(user)
    allowed_class(db, user, class_id)
    reader = csv.DictReader(io.StringIO(body.csv))
    if reader.fieldnames != ["learner_id", "name"]:
        raise HTTPException(422, "Roster columns must be learner_id,name")
    rows, seen = [], set()
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise HTTPException(422, "Malformed roster")
        lid, name = row["learner_id"].strip(), row["name"].strip()
        if not lid or len(lid) > 80 or not name or len(name) > 200 or lid in seen or db.get(Learner, lid):
            raise HTTPException(422, "Invalid, duplicate or unavailable learner ID")
        seen.add(lid)
        rows.append(Learner(id=lid, name=name, class_id=class_id))
        if len(rows) > 5000:
            raise HTTPException(422, "Maximum 5000 learners per upload")
    if not rows:
        raise HTTPException(422, "Roster is empty")
    db.add_all(rows)
    audit(db, user, class_id, "import_roster", {"rows": len(rows)})
    wake_monitor(db, class_id)
    db.commit()
    return {"inserted": len(rows)}


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.3.0"}


@app.get("/ready")
def ready(db=Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"database": "ready"}


@app.post("/auth/login")
def login(body: Login, request: Request, db=Depends(get_db)):
    key = (request.client.host if request.client else "unknown", body.email.lower())
    now = time.time()
    recent = attempts[key]
    while recent and recent[0] < now - 300:
        recent.popleft()
    if len(recent) >= 10:
        raise HTTPException(429, "Too many attempts. Try again in five minutes.")
    recent.append(now)
    u = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not u or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_urlsafe(48)
    db.add(LoginSession(token_hash=token_hash(token), user_id=u.id, expires=now + 8 * 3600))
    db.commit()
    return {"access_token": token, "role": u.role, "expires_in": 28800}


@app.post("/auth/logout")
def logout(request: Request, user=Depends(current_user), db=Depends(get_db)):
    token = request.headers.get("Authorization", "").split(" ", 1)[1]
    login = db.get(LoginSession, token_hash(token))
    if login:
        db.delete(login)
        db.commit()
    return {"signed_out": True}


@app.get("/classes")
def classes(user=Depends(current_user), db=Depends(get_db)):
    query = select(SchoolClass).where(SchoolClass.school == user.school)
    if user.role != "principal":
        query = query.where(SchoolClass.teacher_id == user.id)
    return [{"id": c.id, "name": c.name, "subject": c.subject} for c in db.scalars(query)]


@app.get("/classes/{class_id}/learners")
def learners(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    return [{"id": l.id, "name": l.name} for l in db.scalars(select(Learner).where(Learner.class_id == class_id))]


@app.post("/classes/{class_id}/imports/{kind}")
def upload(class_id: int, kind: Literal["marks", "attendance"], body: Upload, user=Depends(current_user), db=Depends(get_db)):
    return import_csv(db, user, class_id, body.csv, kind)


@app.get("/classes/{class_id}/evidence")
def get_evidence(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    return evidence(db, user, class_id)


@app.patch("/classes/{class_id}/marks/{mark_id}")
def correct(class_id: int, mark_id: int, body: Correction, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    mark = db.get(Mark, mark_id)
    if not mark or mark.class_id != class_id:
        raise HTTPException(404, "Mark not found")
    if body.earned is not None and not 0 <= body.earned <= mark.possible:
        raise HTTPException(422, "Mark outside range")
    audit(db, user, class_id, "correct_mark", {"mark_id": mark_id, "before": mark.earned, "after": body.earned, "reason": body.reason})
    changed = mark.earned != body.earned
    mark.earned = body.earned
    wake_monitor(db, class_id)
    from .captures import record_capture
    capture_id = record_capture(db, user, class_id, "mark_correction", int(changed))
    db.commit()
    return {"corrected": mark_id, "capture_id": capture_id}


@app.get("/classes/{class_id}/marks")
def mark_records(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    return [{"id": m.id, "learner_id": m.learner_id, "assessment": m.assessment, "topic": m.topic, "earned": m.earned, "possible": m.possible} for m in db.scalars(select(Mark).where(Mark.class_id == class_id))]


@app.post("/classes/{class_id}/interventions")
def create_support(class_id: int, body: Support, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    learner = db.get(Learner, body.learner_id)
    if not learner or learner.class_id != class_id:
        raise HTTPException(422, "Learner does not belong to this class")
    item = Intervention(class_id=class_id, learner_id=body.learner_id, topic=body.topic, activity=body.activity, due=body.due.isoformat())
    db.add(item)
    audit(db, user, class_id, "assign_support", body.model_dump(mode="json"))
    wake_monitor(db, class_id)
    db.commit()
    return {"id": item.id}


@app.patch("/classes/{class_id}/interventions/{item_id}")
def update_support(class_id: int, item_id: int, body: FollowUp, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    item = db.get(Intervention, item_id)
    if not item or item.class_id != class_id:
        raise HTTPException(404, "Intervention not found")
    if body.status == "completed" and not body.follow_up.strip():
        raise HTTPException(422, "Record completion evidence or follow-up before closing")
    item.status, item.follow_up = body.status, body.follow_up
    audit(db, user, class_id, "update_support", {"id": item_id, **body.model_dump()})
    wake_monitor(db, class_id)
    db.commit()
    return {"updated": item_id}


@app.get("/classes/{class_id}/audit")
def get_audit(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    return [{"at": a.at, "action": a.action, "user_id": a.user_id, "detail": a.detail} for a in db.scalars(select(Audit).where(Audit.class_id == class_id).order_by(Audit.id.desc()).limit(200))]

@app.get("/agent-runtime")
def agent_runtime(user=Depends(current_user), db=Depends(get_db)):
    from .providers import provider_details, configured
    from .config import settings
    config = settings()
    return {"provider": config.ai_provider, "model": provider_details()[1],
            "configured": configured(), "fallback_provider": config.ai_fallback_provider,
            "available_providers": {name: configured(name) for name in ["gemini", "groq", "offline"]},
            "worker_last_seen": db.scalar(select(func.max(WorkerHeartbeat.last_seen))),
            "autonomous_daily_report_limit": config.autonomous_daily_report_limit}


@app.get("/classes/{class_id}/statistics")
def statistics(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    from .jobs import baseline_report
    return baseline_report(evidence(db, user, class_id))


@app.get("/classes/{class_id}/runs")
def get_runs(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    from .jobs import public_run
    allowed_class(db, user, class_id)
    return [public_run(db, run) for run in db.scalars(select(AgentRun).where(
        AgentRun.class_id == class_id).order_by(AgentRun.at.desc()).limit(20))]


def scoped_run(db, user, class_id, identifier):
    allowed_class(db, user, class_id)
    run = db.get(AgentRun, identifier)
    if not run or run.class_id != class_id:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/classes/{class_id}/runs/{identifier}")
def get_run(class_id: int, identifier: str, user=Depends(current_user), db=Depends(get_db)):
    from .jobs import public_run
    return public_run(db, scoped_run(db, user, class_id, identifier))


@app.post("/classes/{class_id}/runs", status_code=202)
def run_agents(class_id: int, body: Goal, user=Depends(current_user), db=Depends(get_db)):
    from .providers import configured
    from .jobs import enqueue, public_run
    allowed_class(db, user, class_id)
    if not configured(body.provider):
        raise HTTPException(503, "Configure the selected provider's API key in Codespaces secrets and restart, or choose offline.")
    if body.demonstrate_repair and (body.provider or settings_provider()) != "offline":
        raise HTTPException(422, "The self-correction demonstration runs only with the offline provider.")
    active = db.scalar(select(AgentRun.id).join(AgentJob).where(
        AgentRun.class_id == class_id, AgentJob.status.in_(["queued", "running", "retrying"])))
    if active:
        raise HTTPException(409, "A report is already active for this class. Open its saved progress.")
    try:
        run = enqueue(db, user, class_id, body.goal, provider=body.provider,
                      demonstrate_repair=body.demonstrate_repair)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A report became active for this class. Refresh its progress.") from None
    return public_run(db, run)


@app.post("/classes/{class_id}/runs/{identifier}/resume", status_code=202)
def resume_run(class_id: int, identifier: str, body: ResumeRun,
               user=Depends(current_user), db=Depends(get_db)):
    from .jobs import resume, public_run
    from .providers import configured
    run = scoped_run(db, user, class_id, identifier)
    job = db.get(AgentJob, run.id)
    if not job:
        raise HTTPException(409, "This historical run has no stage checkpoints; start a new report.")
    provider = body.provider or job.provider
    if not configured(provider):
        raise HTTPException(503, "Configure the selected provider's API key before resuming.")
    active = db.scalar(select(AgentRun.id).join(AgentJob).where(
        AgentRun.class_id == class_id, AgentRun.id != run.id,
        AgentJob.status.in_(["queued", "running", "retrying"])))
    if active:
        raise HTTPException(409, "Another report is already active for this class.")
    try:
        resume(db, run, provider)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Another report became active for this class.") from None
    audit(db, user, class_id, "agent_job_resumed", {"run_id": run.id, "provider": provider})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Another report became active for this class.") from None
    return public_run(db, run)


@app.post("/classes/{class_id}/runs/{identifier}/cancel")
def cancel_run(class_id: int, identifier: str, user=Depends(current_user), db=Depends(get_db)):
    from .jobs import public_run
    run = scoped_run(db, user, class_id, identifier)
    job = db.get(AgentJob, run.id)
    if not job or job.status not in {"queued", "running", "retrying"}:
        raise HTTPException(409, "This run is not active")
    job.status, job.lease_owner, job.lease_until = "cancelled", "", 0
    job.active_class_id = None
    run.status = "cancelled"
    run.result = {"message": "Further processing cancelled. Completed stages remain saved."}
    from .captures import job_updated
    job_updated(db, run, job)
    audit(db, user, class_id, "agent_job_cancelled", {"run_id": run.id})
    db.commit()
    return public_run(db, run)


@app.get("/classes/{class_id}/monitoring")
def get_monitoring(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    policy = db.get(MonitoringPolicy, class_id)
    if not policy:
        return PolicyBody().model_dump()
    return {name: getattr(policy, name) for name in PolicyBody.model_fields}


@app.put("/classes/{class_id}/monitoring")
def set_monitoring(class_id: int, body: PolicyBody, user=Depends(current_user), db=Depends(get_db)):
    from .providers import configured
    principal(user)
    allowed_class(db, user, class_id)
    if body.enabled and body.use_ai and not configured():
        raise HTTPException(503, "Configure an AI provider before enabling automatic AI reports.")
    policy = db.get(MonitoringPolicy, class_id)
    if not policy:
        policy = MonitoringPolicy(class_id=class_id, owner_id=user.id)
        db.add(policy)
    for name, value in body.model_dump().items():
        setattr(policy, name, value)
    policy.owner_id, policy.next_at, policy.last_hash = user.id, 0, ""
    audit(db, user, class_id, "monitoring_policy_updated", body.model_dump())
    db.commit()
    return body.model_dump()


@app.get("/classes/{class_id}/follow-ups")
def follow_ups(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    return [{"id": item.id, "issue_key": item.issue_key, "status": item.status,
             "detail": item.detail, "outcome": item.outcome, "assigned_to": item.assigned_to,
             "due_at": item.due_at, "escalated_to_principal": item.escalated}
            for item in db.scalars(select(MonitorFollowUp).where(MonitorFollowUp.class_id == class_id))]


@app.patch("/classes/{class_id}/follow-ups/{identifier}")
def update_follow_up(class_id: int, identifier: int, body: FollowUpOutcome,
                     user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    item = db.get(MonitorFollowUp, identifier)
    if not item or item.class_id != class_id:
        raise HTTPException(404, "Follow-up not found")
    item.status, item.outcome, item.updated_at = body.status, body.outcome, utcnow()
    audit(db, user, class_id, "monitor_follow_up_updated", {"id": identifier, **body.model_dump()})
    db.commit()
    return {"updated": identifier}


class SourceBody(StrictBody):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["marks", "attendance"] = "marks"
    allow_corrections: bool = False


class SourceSettings(StrictBody):
    enabled: bool = True
    allow_corrections: bool = False


class EmailPreferenceBody(StrictBody):
    email_enabled: bool = False


@app.get("/classes/{class_id}/sources")
def get_sources(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    from .db import AssessmentSource
    from .ingestion import public_source
    allowed_class(db, user, class_id)
    return [public_source(s) for s in db.scalars(select(AssessmentSource).where(AssessmentSource.class_id == class_id))]


@app.post("/classes/{class_id}/sources", status_code=201)
def register_source(class_id: int, body: SourceBody, user=Depends(current_user), db=Depends(get_db)):
    from .ingestion import create_source, public_source
    source, key = create_source(db, user, class_id, **body.model_dump())
    result = {**public_source(source), "ingestion_key": key}
    db.commit()
    return result


def scoped_source(db, user, class_id, identifier):
    from .db import AssessmentSource
    allowed_class(db, user, class_id)
    source = db.get(AssessmentSource, identifier)
    if not source or source.class_id != class_id:
        raise HTTPException(404, "Source not found")
    return source


@app.put("/classes/{class_id}/sources/{identifier}")
def configure_source(class_id: int, identifier: str, body: SourceSettings,
                     user=Depends(current_user), db=Depends(get_db)):
    from .ingestion import public_source
    source = scoped_source(db, user, class_id, identifier)
    for key, value in body.model_dump().items():
        setattr(source, key, value)
    audit(db, user, class_id, "assessment_source_updated", {"source_id": identifier, **body.model_dump()})
    db.commit()
    return public_source(source)


@app.post("/classes/{class_id}/sources/{identifier}/rotate-key")
def rotate_source_key(class_id: int, identifier: str, user=Depends(current_user), db=Depends(get_db)):
    source = scoped_source(db, user, class_id, identifier)
    key = secrets.token_urlsafe(48)
    source.ingestion_token_hash = token_hash(key)
    audit(db, user, class_id, "source_key_rotated", {"source_id": identifier})
    db.commit()
    return {"ingestion_key": key}


@app.post("/classes/{class_id}/sources/{identifier}/batches", status_code=202)
def upload_source(class_id: int, identifier: str, body: Upload,
                  user=Depends(current_user), db=Depends(get_db)):
    from .ingestion import accept
    source = scoped_source(db, user, class_id, identifier)
    try:
        batch = accept(db, source, body.csv)
        db.commit()
    except IntegrityError:
        db.rollback()
        batch = accept(db, source, body.csv)
    return {"batch_id": batch.id, "status": batch.status}


@app.post("/ingestion/{identifier}", status_code=202)
async def source_feed(identifier: str, request: Request, db=Depends(get_db)):
    from .ingestion import authenticate_source, accept, MAX_BYTES
    source = authenticate_source(db, identifier, request.headers.get("X-Ingestion-Key", ""))
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BYTES:
            raise HTTPException(413, "Maximum CSV size is 2 MB")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeError:
        raise HTTPException(422, "Use UTF-8 CSV") from None
    try:
        batch = accept(db, source, decoded)
        db.commit()
    except IntegrityError:
        db.rollback()
        batch = accept(db, source, decoded)
    return {"batch_id": batch.id, "status": batch.status}


@app.get("/classes/{class_id}/ingestion-batches")
def get_batches(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    from .db import IngestionBatch, AssessmentSource
    allowed_class(db, user, class_id)
    return [{"id": b.id, "source_id": b.source_id, "filename": b.filename, "status": b.status,
             "result": b.result, "created_at": b.created_at} for b in db.scalars(
        select(IngestionBatch).join(AssessmentSource).where(AssessmentSource.class_id == class_id)
        .order_by(IngestionBatch.created_at.desc()).limit(50))]


@app.get("/classes/{class_id}/captures")
def get_captures(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    from .db import CaptureEvent
    allowed_class(db, user, class_id)
    return [{"id": c.id, "created_at": c.created_at, "changed_records": c.changed_records,
             "kind": c.kind, "evaluation_status": c.evaluation_status, "run_id": c.run_id,
             "data_report": "delivered_to_staff_inboxes"} for c in db.scalars(
        select(CaptureEvent).where(CaptureEvent.class_id == class_id)
        .order_by(CaptureEvent.created_at.desc()).limit(50))]


@app.get("/notifications")
def get_notifications(user=Depends(current_user), db=Depends(get_db)):
    from .db import ReportNotification, SchoolClass
    from .notifications import public_notification
    query = select(ReportNotification).join(SchoolClass).where(
        ReportNotification.recipient_id == user.id, SchoolClass.school == user.school)
    if user.role != "principal":
        query = query.where(SchoolClass.teacher_id == user.id)
    return [public_notification(db, n) for n in db.scalars(query.order_by(
        ReportNotification.created_at.desc()).limit(100))]


@app.post("/notifications/{identifier}/read")
def read_notification(identifier: str, user=Depends(current_user), db=Depends(get_db)):
    from .db import ReportNotification
    item = db.get(ReportNotification, identifier)
    if not item or item.recipient_id != user.id:
        raise HTTPException(404, "Report not found")
    allowed_class(db, user, item.class_id)
    item.read_at = utcnow()
    db.commit()
    return {"read": identifier}


@app.get("/notification-preferences")
def email_preferences(user=Depends(current_user), db=Depends(get_db)):
    from .db import NotificationPreference
    from .notifications import smtp_ready, usable_email
    preference = db.get(NotificationPreference, user.id)
    return {"email_enabled": bool(preference and preference.email_enabled),
            "registered_email": user.email, "usable_address": usable_email(user.email),
            "smtp_configured": smtp_ready(), "in_app_delivery": "always_on"}


def save_email_preference(db, user, target, enabled):
    from .db import NotificationPreference
    from .notifications import usable_email
    if enabled and not usable_email(target.email):
        raise HTTPException(422, "Email delivery needs a real registered staff address; sample addresses cannot receive mail.")
    item = db.get(NotificationPreference, target.id)
    if not item:
        item = NotificationPreference(user_id=target.id)
        db.add(item)
    item.email_enabled = enabled
    school_class = db.scalar(select(SchoolClass).where(SchoolClass.school == user.school).limit(1))
    if school_class:
        audit(db, user, school_class.id, "email_preference_updated", {"staff_id": target.id, "email_enabled": enabled})
    db.commit()
    return {"email_enabled": enabled}


@app.put("/notification-preferences")
def set_email_preference(body: EmailPreferenceBody, user=Depends(current_user), db=Depends(get_db)):
    return save_email_preference(db, user, user, body.email_enabled)


@app.put("/staff/{staff_id}/notification-preferences")
def set_staff_email_preference(staff_id: int, body: EmailPreferenceBody,
                              user=Depends(current_user), db=Depends(get_db)):
    principal(user)
    target = db.get(User, staff_id)
    if not target or target.school != user.school:
        raise HTTPException(404, "Staff not found")
    return save_email_preference(db, user, target, body.email_enabled)
