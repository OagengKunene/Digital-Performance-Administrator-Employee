from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from pae.db import Base, User, SchoolClass, AgentRun


def test_additive_table_initialisation_preserves_old_accounts_and_reports():
    engine = create_engine("sqlite://")
    old = [table for table in Base.metadata.sorted_tables if table.name not in {
        "agent_jobs", "monitoring_policies", "monitor_followups",
        "worker_heartbeats", "automatic_report_budgets"}]
    Base.metadata.create_all(engine, tables=old)
    with Session(engine) as db:
        user = User(email="existing@example.test", password_hash="existing-hash", role="principal", school="Existing")
        db.add(user)
        db.flush()
        school_class = SchoolClass(name="10A", subject="Maths", teacher_id=user.id, school="Existing")
        db.add(school_class)
        db.flush()
        db.add(AgentRun(id="historical-run", class_id=school_class.id, user_id=user.id,
            status="failed", goal="Historical request", result={"message": "Old result"}, events=[]))
        db.commit()
    Base.metadata.create_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert db.scalar(select(User)).password_hash == "existing-hash"
        assert db.get(AgentRun, "historical-run").result == {"message": "Old result"}
    engine.dispose()
