import time
from sqlalchemy import select, func
from pae.db import AgentJob, AgentRun, FollowUp, MonitoringPolicy
from pae.monitoring import check_due
from pae.jobs import process_one
import asyncio


def enable(client, principal, use_ai=False, **overrides):
    payload = {"enabled": True, "use_ai": use_ai, "on_record_change": True,
               "interval_minutes": 1440, "daily_ai_limit": 1, **overrides}
    result = client.put("/classes/1/monitoring", headers=principal, json=payload)
    assert result.status_code == 200, result.text


def due(factory):
    with factory() as db:
        policy = db.get(MonitoringPolicy, 1)
        policy.next_at = 0
        db.commit()


def test_monitoring_is_opt_in_and_principal_controlled(client, teacher, principal, db_factory):
    assert not check_due(db_factory)
    response = client.put("/classes/1/monitoring", headers=teacher, json={"enabled": True})
    assert response.status_code == 403
    assert client.put("/classes/1/monitoring", headers=principal,
        json={"enabled": True, "interval_minutes": 0}).status_code == 422


def test_monitor_creates_followups_without_any_model_call(client, teacher, principal, db_factory):
    enable(client, principal)
    assert check_due(db_factory)
    items = client.get("/classes/1/follow-ups", headers=teacher).json()
    assert len(items) == 1
    assert items[0]["issue_key"] == "missing_marks"
    assert items[0]["assigned_to"] == 1
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentJob)) == 0
    due(db_factory)
    check_due(db_factory)
    assert len(client.get("/classes/1/follow-ups", headers=teacher).json()) == 1


def test_record_change_wakes_monitor_and_closes_resolved_issue(client, teacher, principal, db_factory):
    enable(client, principal)
    check_due(db_factory)
    records = client.get("/classes/1/marks", headers=teacher).json()
    missing = next(row for row in records if row["earned"] is None)
    assert client.patch(f"/classes/1/marks/{missing['id']}", headers=teacher,
        json={"earned": 20, "reason": "Teacher captured the outstanding script."}).status_code == 200
    with db_factory() as db:
        assert db.get(MonitoringPolicy, 1).next_at == 0
    check_due(db_factory)
    items = client.get("/classes/1/follow-ups", headers=teacher).json()
    assert items[0]["status"] == "resolved"
    assert "Automatically closed" in items[0]["outcome"]


def test_overdue_followups_escalate_to_principal(client, principal, db_factory):
    enable(client, principal)
    now = time.time()
    check_due(db_factory, now=now)
    due(db_factory)
    check_due(db_factory, now=now + 86401)
    items = client.get("/classes/1/follow-ups", headers=principal).json()
    assert items[0]["escalated_to_principal"] is True


def test_followup_outcomes_are_class_scoped_and_audited(client, teacher, principal, db_factory):
    enable(client, principal)
    check_due(db_factory)
    item = client.get("/classes/1/follow-ups", headers=teacher).json()[0]
    body = {"status": "acknowledged", "outcome": "Teacher will verify the original script."}
    assert client.patch(f"/classes/2/follow-ups/{item['id']}", headers=teacher, json=body).status_code == 404
    assert client.patch(f"/classes/1/follow-ups/{item['id']}", headers=teacher, json=body).status_code == 200
    audits = client.get("/classes/1/audit", headers=principal).json()
    assert any(row["action"] == "monitor_follow_up_updated" for row in audits)


def test_changed_evidence_initiates_goal_without_manual_prompt(client, principal, ai_config, db_factory):
    enable(client, principal, use_ai=True)
    check_due(db_factory)
    with db_factory() as db:
        jobs = db.scalars(select(AgentJob)).all()
        assert len(jobs) == 1
        assert jobs[0].state["trigger"] == "monitor"
        run = db.get(AgentRun, jobs[0].id)
        assert "Investigate changed evidence" in run.goal
    due(db_factory)
    check_due(db_factory)
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentJob)) == 1


def test_disabling_automation_blocks_queued_automatic_work(client, principal, ai_config, db_factory):
    enable(client, principal, use_ai=True)
    check_due(db_factory)
    assert client.put("/classes/1/monitoring", headers=principal,
        json={"enabled": False, "use_ai": False}).status_code == 200

    async def forbidden(*args, **kwargs):
        raise AssertionError("Disabled automation dispatched a model call")
    asyncio.run(process_one(db_factory, forbidden))
    with db_factory() as db:
        assert db.scalar(select(AgentRun)).status == "cancelled"


def test_installation_daily_cap_is_shared_between_classes(client, principal, ai_config, db_factory):
    from pae.db import AutomaticReportBudget
    enable(client, principal, use_ai=True)
    check_due(db_factory)
    assert client.put("/classes/2/monitoring", headers=principal,
        json={"enabled": True, "use_ai": True}).status_code == 200
    check_due(db_factory)
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentJob)) == 1
        assert db.scalar(select(AutomaticReportBudget)).used == 1


def test_monitor_assigns_support_review_from_actual_threshold(client, teacher, principal, db_factory):
    records = client.get("/classes/1/marks", headers=teacher).json()
    mark = next(row for row in records if row["learner_id"] == "L1")
    client.patch(f"/classes/1/marks/{mark['id']}", headers=teacher,
                 json={"earned": 5, "reason": "Teacher verified the source mark."})
    enable(client, principal)
    check_due(db_factory)
    items = client.get("/classes/1/follow-ups", headers=teacher).json()
    support = next(item for item in items if item["issue_key"] == "support:L1")
    assert support["detail"]["mean_percentage"] == 25
    assert support["assigned_to"] == 1
