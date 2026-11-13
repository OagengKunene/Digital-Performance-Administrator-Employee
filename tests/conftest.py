import time
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from pae.db import Base, get_db, User, SchoolClass, Learner, Mark, LoginSession
from pae.api import app, attempts
from pae.security import hash_password, token_hash


@pytest.fixture
def client():
    pg_url = os.environ.get("TEST_DATABASE_URL")
    if pg_url:
        from sqlalchemy.engine import make_url
        assert make_url(pg_url).database == "pae_test", "Only the disposable pae_test database may be reset"
        engine = create_engine(pg_url, pool_pre_ping=True)
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([User(email="teacher@example.test", password_hash=hash_password("a-long-test-password"), role="teacher", school="One"),
                    User(email="principal@example.test", password_hash=hash_password("a-long-test-password"), role="principal", school="One"),
                    User(email="other@example.test", password_hash=hash_password("a-long-test-password"), role="teacher", school="One"),
                    User(email="outsider@example.test", password_hash=hash_password("a-long-test-password"), role="principal", school="Two")])
        db.flush()
        db.add_all([SchoolClass(school="One", name="10A", subject="Maths", teacher_id=1), SchoolClass(school="One", name="10B", subject="Maths", teacher_id=3), SchoolClass(school="Two", name="11A", subject="Maths", teacher_id=4)])
        db.flush()
        for i, score in enumerate([12, 15, 18, None], 1):
            db.add(Learner(id=f"L{i}", class_id=1, name=f"Learner {i}"))
            db.flush()
            db.add(Mark(class_id=1, learner_id=f"L{i}", assessment="Test 1", topic="Algebra", earned=score, possible=20))
        for token, uid in [("teacher-token", 1), ("principal-token", 2), ("other-token", 3), ("outsider-token", 4)]:
            db.add(LoginSession(token_hash=token_hash(token), user_id=uid, expires=time.time() + 3600))
        db.commit()
    def session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    attempts.clear()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
def teacher():
    return {"Authorization": "Bearer teacher-token"}


@pytest.fixture
def principal():
    return {"Authorization": "Bearer principal-token"}
