from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, String, ForeignKey, UniqueConstraint, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str]
    role: Mapped[str]
    school: Mapped[str]


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires: Mapped[float]


class SchoolClass(Base):
    __tablename__ = "classes"
    id: Mapped[int] = mapped_column(primary_key=True)
    school: Mapped[str]
    name: Mapped[str]
    subject: Mapped[str]
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    support_threshold: Mapped[float] = mapped_column(default=50.0)


class Learner(Base):
    __tablename__ = "learners"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    name: Mapped[str]


class Mark(Base):
    __tablename__ = "marks"
    __table_args__ = (UniqueConstraint("class_id", "learner_id", "assessment", "topic"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    learner_id: Mapped[str] = mapped_column(ForeignKey("learners.id"))
    assessment: Mapped[str]
    topic: Mapped[str]
    earned: Mapped[float | None]
    possible: Mapped[float]


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (UniqueConstraint("learner_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    learner_id: Mapped[str] = mapped_column(ForeignKey("learners.id"))
    day: Mapped[str]
    status: Mapped[str]


class Intervention(Base):
    __tablename__ = "interventions"
    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    learner_id: Mapped[str] = mapped_column(ForeignKey("learners.id"))
    topic: Mapped[str]
    activity: Mapped[str]
    due: Mapped[str]
    status: Mapped[str] = mapped_column(default="assigned")
    follow_up: Mapped[str] = mapped_column(default="")


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    action: Mapped[str]
    detail: Mapped[dict] = mapped_column(JSON)
    at: Mapped[str] = mapped_column(default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str]
    goal: Mapped[str]
    result: Mapped[dict] = mapped_column(JSON)
    events: Mapped[list] = mapped_column(JSON)
    at: Mapped[str] = mapped_column(default=utcnow)


class AgentJob(Base):
    __tablename__ = "agent_jobs"
    id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    status: Mapped[str] = mapped_column(default="queued", index=True)
    provider: Mapped[str]
    model: Mapped[str]
    fallback: Mapped[str] = mapped_column(default="")
    snapshot: Mapped[dict] = mapped_column(JSON)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    active_class_id: Mapped[int | None] = mapped_column(ForeignKey("classes.id"), unique=True, nullable=True)
    next_at: Mapped[float] = mapped_column(default=0.0, index=True)
    lease_owner: Mapped[str] = mapped_column(default="")
    lease_until: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[str] = mapped_column(default=utcnow)


class MonitoringPolicy(Base):
    __tablename__ = "monitoring_policies"
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    enabled: Mapped[bool] = mapped_column(default=False)
    use_ai: Mapped[bool] = mapped_column(default=False)
    on_record_change: Mapped[bool] = mapped_column(default=True)
    interval_minutes: Mapped[int] = mapped_column(default=1440)
    daily_ai_limit: Mapped[int] = mapped_column(default=1)
    next_at: Mapped[float] = mapped_column(default=0.0)
    last_hash: Mapped[str] = mapped_column(default="")


class FollowUp(Base):
    __tablename__ = "monitor_followups"
    __table_args__ = (UniqueConstraint("class_id", "issue_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    issue_key: Mapped[str] = mapped_column(String(120))
    assigned_to: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(default="open")
    detail: Mapped[dict] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(default="")
    due_at: Mapped[float]
    escalated: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[str] = mapped_column(default=utcnow)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    last_seen: Mapped[float]


class AutomaticReportBudget(Base):
    __tablename__ = "automatic_report_budgets"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    used: Mapped[int] = mapped_column(default=0)


class AssessmentSource(Base):
    __tablename__ = "assessment_sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    kind: Mapped[str] = mapped_column(default="marks")
    enabled: Mapped[bool] = mapped_column(default=True)
    allow_corrections: Mapped[bool] = mapped_column(default=False)
    ingestion_token_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(default=utcnow)


class IngestionBatch(Base):
    __tablename__ = "ingestion_batches"
    __table_args__ = (UniqueConstraint("source_id", "content_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("assessment_sources.id"))
    filename: Mapped[str]
    content: Mapped[str]
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(default="pending")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(default=utcnow)
    next_at: Mapped[float] = mapped_column(default=0.0)


class CaptureEvent(Base):
    __tablename__ = "capture_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str]
    changed_records: Mapped[int]
    snapshot: Mapped[dict] = mapped_column(JSON)
    source_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(default=utcnow)
    evaluation_status: Mapped[str] = mapped_column(default="pending")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True, unique=True)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    email_enabled: Mapped[bool] = mapped_column(default=False)


class ReportNotification(Base):
    __tablename__ = "report_notifications"
    __table_args__ = (UniqueConstraint("recipient_id", "dedupe_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    dedupe_key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str]
    title: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(default=utcnow)
    read_at: Mapped[str | None] = mapped_column(nullable=True)


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"
    id: Mapped[str] = mapped_column(ForeignKey("report_notifications.id"), primary_key=True)
    status: Mapped[str] = mapped_column(default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_at: Mapped[float] = mapped_column(default=0.0)
    lease_owner: Mapped[str] = mapped_column(default="")
    lease_until: Mapped[float] = mapped_column(default=0.0)
    last_error: Mapped[str] = mapped_column(default="")
    sent_at: Mapped[str | None] = mapped_column(nullable=True)


class ScriptPhoto(Base):
    __tablename__ = "script_photos"
    __table_args__ = (UniqueConstraint("class_id", "image_hash", "reader"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str]
    image: Mapped[bytes]
    image_hash: Mapped[str] = mapped_column(String(64))
    reader: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued", index=True)
    extraction: Mapped[dict] = mapped_column(JSON, default=dict)
    original_extraction: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    events: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(default=0)
    next_at: Mapped[float] = mapped_column(default=0.0)
    lease_owner: Mapped[str] = mapped_column(default="")
    lease_until: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[str] = mapped_column(default=utcnow)


def make_engine(url):
    if url.startswith("sqlite"):
        Path(".local").mkdir(exist_ok=True)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


engine = make_engine(settings().database_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        yield db
