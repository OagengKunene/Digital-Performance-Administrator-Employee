import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import date
from typing import Literal
from fastapi import FastAPI, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, text
from .db import get_db, User, LoginSession, SchoolClass, Learner, Mark, Intervention, Audit, AgentRun
from .security import current_user, verify_password, hash_password, token_hash
from .services import allowed_class, audit, evidence, import_csv

app = FastAPI(title="Performance Administrator", version="0.1.0")
attempts = defaultdict(deque)


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
    db.commit()
    return {"inserted": len(rows)}


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


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
    mark.earned = body.earned
    db.commit()
    return {"corrected": mark_id}


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
    db.commit()
    return {"updated": item_id}


@app.get("/classes/{class_id}/audit")
def get_audit(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    return [{"at": a.at, "action": a.action, "user_id": a.user_id, "detail": a.detail} for a in db.scalars(select(Audit).where(Audit.class_id == class_id).order_by(Audit.id.desc()).limit(200))]


@app.get("/classes/{class_id}/runs")
def get_runs(class_id: int, user=Depends(current_user), db=Depends(get_db)):
    allowed_class(db, user, class_id)
    return [{"id": r.id, "at": r.at, "status": r.status, "goal": r.goal, "result": r.result, "events": r.events} for r in db.scalars(select(AgentRun).where(AgentRun.class_id == class_id).order_by(AgentRun.at.desc()).limit(20))]


@app.post("/classes/{class_id}/runs")
async def run_agents(class_id: int, body: Goal, user=Depends(current_user), db=Depends(get_db)):
    from .agents import run_reviewed_report
    from .config import settings
    data = evidence(db, user, class_id)
    if not settings().google_api_key:
        raise HTTPException(503, "Add GOOGLE_API_KEY to Codespaces Secrets and restart the service to enable live agents.")
    run_id = str(uuid.uuid4())
    result = await run_reviewed_report(data, body.goal, run_id)
    db.add(AgentRun(id=run_id, class_id=class_id, user_id=user.id, goal=body.goal, **result))
    audit(db, user, class_id, "agent_run", {"run_id": run_id, "status": result["status"]})
    db.commit()
    return {"id": run_id, **result}
