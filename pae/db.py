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
