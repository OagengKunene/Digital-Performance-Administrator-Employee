"""Offline provider and demo data: the real job pipeline with no API key."""
import asyncio
from sqlalchemy import select
from pae.db import AgentJob, Audit
from pae.jobs import process_one
from pae.agents import SCHEMAS
from pae.offline_engine import offline_stage, STAGES
from pae import demo_data


def drain(factory, maximum=20):
    for _ in range(maximum):
        if not asyncio.run(process_one(factory)):
            return
    raise AssertionError("worker did not become idle")


def start(client, headers, **extra):
    r = client.post("/classes/1/runs", headers=headers,
                    json={"goal": "Review performance and prepare a teacher report.", "provider": "offline", **extra})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def no_keys(monkeypatch):
    from pae.config import settings
    for name, value in {"google_api_key": "", "groq_api_key": "", "ai_provider": "gemini",
                        "ai_fallback_provider": ""}.items():
        monkeypatch.setattr(settings(), name, value)


def test_offline_report_runs_with_no_keys(client, teacher, db_factory, monkeypatch):
    no_keys(monkeypatch)
    identifier = start(client, teacher)
    drain(db_factory)
    run = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert run["status"] == "verified_metrics", run
    assert (run["result"]["mean_percentage"], run["result"]["marked_count"], run["result"]["missing_count"]) == (75, 3, 1)
    assert "3 of 4" in run["result"]["summary"]
    assert run["progress"]["provider"] == "offline" and run["progress"]["repair_count"] == 0
    stages = [e["agent"] for e in run["events"] if e["type"] == "stage_completed"]
    assert stages == ["coordinator", "performance_analyst", "intervention_planner", "reporting_agent", "quality_reviewer"]


def test_demonstration_shows_reviewer_repair(client, teacher, db_factory, monkeypatch):
    no_keys(monkeypatch)
    identifier = start(client, teacher, demonstrate_repair=True)
    drain(db_factory)
    run = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert run["status"] == "verified_metrics", run
    assert run["progress"]["repair_count"] == 1 and run["progress"]["demonstrate_repair"] is True
    kinds = [e["type"] for e in run["events"]]
    assert "repair_requested" in kinds
    assert kinds.index("repair_requested") < len(kinds) - 1
    assert "3 of 4" in run["result"]["summary"]
    with db_factory() as db:
        queued = db.scalar(select(Audit).where(Audit.action == "agent_job_queued"))
        assert queued.detail["demonstrate_repair"] is True and queued.detail["provider"] == "offline"


def test_live_provider_without_key_is_refused_and_demo_needs_offline(client, teacher, monkeypatch):
    no_keys(monkeypatch)
    body = {"goal": "Review performance and prepare a teacher report."}
    assert client.post("/classes/1/runs", headers=teacher, json=body).status_code == 503
    assert client.post("/classes/1/runs", headers=teacher, json={**body, "provider": "groq"}).status_code == 503
    from pae.config import settings
    monkeypatch.setattr(settings(), "groq_api_key", "set")
    assert client.post("/classes/1/runs", headers=teacher,
                       json={**body, "provider": "groq", "demonstrate_repair": True}).status_code == 422
    assert client.post("/classes/1/runs", headers=teacher, json={**body, "provider": "magic"}).status_code == 422


def test_runtime_lists_offline_as_available(client, teacher, monkeypatch):
    no_keys(monkeypatch)
    runtime = client.get("/agent-runtime", headers=teacher).json()
    assert runtime["available_providers"] == {"gemini": False, "groq": False, "offline": True}


def test_live_failure_falls_back_to_offline(client, teacher, db_factory, monkeypatch):
    from pae.config import settings
    for name, value in {"ai_provider": "groq", "groq_api_key": "fake", "ai_fallback_provider": "offline",
                        "ai_retry_delay_seconds": 1}.items():
        monkeypatch.setattr(settings(), name, value)
    calls = []

    async def failing(stage, *args, **kwargs):
        calls.append(stage)
        error = RuntimeError("rate limited")
        error.status_code = 429
        raise error
    monkeypatch.setattr("pae.agents.run_stage", failing)
    monkeypatch.setattr("pae.jobs.run_stage", failing, raising=False)
    identifier = client.post("/classes/1/runs", headers=teacher,
                             json={"goal": "Review performance and prepare a teacher report."}).json()["id"]
    import time
    for _ in range(30):
        asyncio.run(process_one(db_factory))
        with db_factory() as db:
            job = db.get(AgentJob, identifier)
            if job.status in {"verified_metrics", "failed", "requires_review"}:
                break
            job.next_at = 0
            db.commit()
    run = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert calls, "the live provider was tried first"
    assert run["status"] == "verified_metrics", run
    assert any(e.get("provider") == "offline" for e in run["events"] if e["type"] == "stage_started")


def test_every_offline_stage_matches_its_schema_on_edge_cases():
    empty = {"class_name": "Empty", "subject": "Maths", "learner_count": 0, "expected_marks": 0, "marked_count": 0,
             "missing_count": 0, "mean_percentage": None, "topics": [], "learners": [], "support_threshold": 50.0,
             "attendance_recorded_status_counts": {}, "interventions": []}
    outputs = {}
    for stage in STAGES:
        outputs[stage] = asyncio.run(offline_stage(stage, empty, "goal text here", outputs, "offline", "m", "r"))
        SCHEMAS[stage].model_validate(outputs[stage])
    assert outputs["coordinator"]["analyse_performance"] is False
    assert outputs["quality_reviewer"]["approved"] is True


def test_demo_data_is_realistic_deterministic_and_idempotent(db_factory, principal, client):
    from datetime import date
    from pae.services import evidence
    from pae.db import User
    with db_factory() as db:
        creds = demo_data.build(db, today=date(2026, 11, 14))
        assert [c[0] for c in creds] == ["principal", "teacher"]
        assert demo_data.build(db, today=date(2026, 11, 14)) == []
        user = db.scalar(select(User).where(User.email == demo_data.PRINCIPAL))
        from pae.db import SchoolClass
        cid = db.scalar(select(SchoolClass.id).where(SchoolClass.school == demo_data.SCHOOL))
        data = evidence(db, user, cid)
    assert data["learner_count"] == 16 and data["expected_marks"] == 128
    assert data["marked_count"] == 125 and data["missing_count"] == 3
    assert 50 < data["mean_percentage"] < 80
    assert sum(l["support_review"] for l in data["learners"]) >= 2
    assert data["attendance_recorded_status_counts"]["absent"] > 0
    assert {i["status"] for i in data["interventions"]} == {"assigned", "in_progress", "completed"}


def test_demo_credentials_file_is_private(tmp_path):
    file = demo_data.write_credentials([("principal", "a@b.test", "secret-value")], tmp_path)
    assert oct(file.stat().st_mode & 0o777) == "0o600"
    assert "secret-value" in file.read_text()


def test_overdue_support_is_labelled():
    from datetime import date, timedelta
    from pae.offline_engine import intervention_planner
    data = {"learners": [], "topics": [], "support_threshold": 50.0, "interventions": [
        {"id": 1, "learner_id": "A", "topic": "Algebra", "due": (date.today() - timedelta(days=2)).isoformat(), "status": "assigned"},
        {"id": 2, "learner_id": "B", "topic": "Geometry", "due": (date.today() + timedelta(days=2)).isoformat(), "status": "assigned"}]}
    recs = intervention_planner(data, "goal", {})["recommendations"]
    assert "OVERDUE" in recs[0] and "#1" in recs[0]
    assert "OVERDUE" not in recs[1]
