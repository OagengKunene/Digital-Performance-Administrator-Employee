import asyncio
import copy
import time
from collections import Counter
from sqlalchemy import select
from pae.db import AgentJob, AgentRun, Mark
from pae.jobs import process_one, claim, save, LeaseLost
import pytest


def output(stage, *, approve=True, analyse=True, support=True):
    return {
        "coordinator": {"rationale": "Investigate recorded class evidence.",
            "analyse_performance": analyse, "plan_support": support, "focus_topics": ["Algebra"]},
        "performance_analyst": {"findings": ["75% across three marked records; one missing."]},
        "intervention_planner": {"recommendations": ["Verify the original scripts and mark register for the 1 missing topic record(s); capture results only when supported by the source."]},
        "reporting_agent": {"summary": "Class evidence shows three marked records and one missing mark.",
            "mean_percentage": 75.0, "marked_count": 3, "missing_count": 1,
            "recommendations": ["Verify the original scripts and mark register for the 1 missing topic record(s); capture results only when supported by the source."]},
        "quality_reviewer": {"approved": approve, "issues": [] if approve else ["Clarify the evidence limitation."],
            "checked_mean_percentage": 75.0, "checked_marked_count": 3, "checked_missing_count": 1, "checked_assessment_count": 1},
    }[stage]


class Executor:
    def __init__(self, fail=None, reject_once=False, analyse=True, support=True):
        self.calls = []
        self.fail = fail
        self.reject_once = reject_once
        self.analyse, self.support = analyse, support

    async def __call__(self, stage, data, goal, outputs, provider, model, request_id, on_request=None):
        self.calls.append((stage, provider))
        if on_request:
            await on_request({"type": "model_request", "agent": stage, "provider": provider})
        if self.fail and self.fail(stage, provider):
            error = RuntimeError("private provider details")
            error.status_code = 503
            raise error
        approve = not (self.reject_once and stage == "quality_reviewer"
                       and Counter(name for name, _ in self.calls)[stage] == 1)
        return copy.deepcopy(output(stage, approve=approve, analyse=self.analyse, support=self.support))


def queued(client, teacher):
    response = client.post("/classes/1/runs", headers=teacher,
                           json={"goal": "Review performance and prepare a teacher report."})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def drain(factory, executor, maximum=15):
    for _ in range(maximum):
        if not asyncio.run(process_one(factory, executor)):
            return
    raise AssertionError("Worker failed to become idle")


def test_report_is_queued_without_running_in_api(client, teacher, ai_config):
    identifier = queued(client, teacher)
    run = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert run["status"] == "queued"
    assert run["progress"]["completed_stages"] == []
    assert run["result"]["statistical_report"]["mean_percentage"] == 75
    assert client.post("/classes/1/runs", headers=teacher,
                       json={"goal": "Another report for this class."}).status_code == 409


def test_coordinator_delegates_and_verified_report_survives_new_session(client, teacher, ai_config, db_factory):
    identifier = queued(client, teacher)
    executor = Executor()
    drain(db_factory, executor)
    run = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert run["status"] == "verified_metrics", run
    assert run["result"]["mean_percentage"] == 75
    assert [name for name, _ in executor.calls] == [
        "coordinator", "performance_analyst", "intervention_planner", "reporting_agent", "quality_reviewer"]
    assert run["progress"]["request_attempts"] == 5
    with db_factory() as db:
        assert db.get(AgentJob, identifier).active_class_id is None


def test_coordinator_can_select_relevant_specialists(client, teacher, ai_config, db_factory):
    identifier = queued(client, teacher)
    executor = Executor(analyse=False, support=False)
    drain(db_factory, executor)
    assert [name for name, _ in executor.calls] == ["coordinator", "reporting_agent", "quality_reviewer"]
    assert client.get("/classes/1/runs/" + identifier, headers=teacher).json()["status"] == "verified_metrics"


def test_failed_reporting_resumes_without_repeating_completed_agents(client, teacher, ai_config, db_factory):
    identifier = queued(client, teacher)
    executor = Executor(fail=lambda stage, provider: stage == "reporting_agent")
    for _ in range(6):
        asyncio.run(process_one(db_factory, executor))
        with db_factory() as db:
            job = db.get(AgentJob, identifier)
            job.next_at = 0
            db.commit()
    failed = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert failed["status"] == "failed"
    assert failed["progress"]["completed_stages"] == ["coordinator", "performance_analyst", "intervention_planner"]
    assert "private provider details" not in str(failed)
    response = client.post("/classes/1/runs/" + identifier + "/resume", headers=teacher, json={})
    assert response.status_code == 202
    executor.fail = None
    drain(db_factory, executor)
    counts = Counter(name for name, _ in executor.calls)
    assert counts["coordinator"] == counts["performance_analyst"] == counts["intervention_planner"] == 1
    assert client.get("/classes/1/runs/" + identifier, headers=teacher).json()["status"] == "verified_metrics"


def test_reviewer_repairs_only_report_and_review(client, teacher, ai_config, db_factory):
    identifier = queued(client, teacher)
    executor = Executor(reject_once=True)
    drain(db_factory, executor)
    counts = Counter(name for name, _ in executor.calls)
    assert counts["coordinator"] == counts["performance_analyst"] == counts["intervention_planner"] == 1
    assert counts["reporting_agent"] == counts["quality_reviewer"] == 2
    assert client.get("/classes/1/runs/" + identifier, headers=teacher).json()["status"] == "verified_metrics"


def test_explicit_fallback_applies_only_to_failed_stage(client, teacher, ai_config, db_factory, monkeypatch):
    monkeypatch.setattr(ai_config, "ai_fallback_provider", "groq")
    monkeypatch.setattr(ai_config, "groq_api_key", "fake-test-key")
    identifier = queued(client, teacher)
    executor = Executor(fail=lambda stage, provider: stage == "reporting_agent" and provider == "gemini")
    for _ in range(4):
        asyncio.run(process_one(db_factory, executor))
    with db_factory() as db:
        db.get(AgentJob, identifier).next_at = 0
        db.commit()
    drain(db_factory, executor)
    assert ("reporting_agent", "groq") in executor.calls
    assert executor.calls[-1] == ("quality_reviewer", "gemini")
    assert client.get("/classes/1/runs/" + identifier, headers=teacher).json()["status"] == "verified_metrics"


def test_expired_lease_reclaims_and_fences_old_worker(client, teacher, ai_config, db_factory):
    identifier = queued(client, teacher)
    first = claim(db_factory)
    assert claim(db_factory) is None
    with db_factory() as db:
        db.get(AgentJob, identifier).lease_until = time.time() - 1
        db.commit()
    second = claim(db_factory)
    assert first[1] != second[1]
    with pytest.raises(LeaseLost):
        save(db_factory, identifier, first[1], {}, "verified_metrics", {}, event={"type": "invalid"})
    with db_factory() as db:
        assert db.get(AgentRun, identifier).status == "running"


def test_cancelled_work_cannot_publish(client, teacher, ai_config, db_factory):
    identifier = queued(client, teacher)
    first = claim(db_factory)
    assert client.post("/classes/1/runs/" + identifier + "/cancel", headers=teacher).status_code == 200
    with pytest.raises(LeaseLost):
        save(db_factory, identifier, first[1], {}, "verified_metrics", {}, event={"type": "invalid"})
    assert client.get("/classes/1/runs/" + identifier, headers=teacher).json()["status"] == "cancelled"


def test_stale_snapshot_blocks_publication(client, teacher, ai_config, db_factory):
    identifier = queued(client, teacher)
    executor = Executor()
    for _ in range(4):
        asyncio.run(process_one(db_factory, executor))
    with db_factory() as db:
        db.scalar(select(Mark).where(Mark.learner_id == "L1")).earned = 10
        db.commit()
    drain(db_factory, executor)
    run = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert run["status"] == "requires_review"
    assert run["result"]["error_code"] == "snapshot_changed"


def test_run_progress_is_class_scoped(client, teacher, principal, ai_config):
    identifier = queued(client, teacher)
    for path in ["", "/resume", "/cancel"]:
        method = client.get if not path else client.post
        response = method("/classes/2/runs/" + identifier + path, headers=teacher,
                          **({"json": {}} if path == "/resume" else {}))
        assert response.status_code == 404
    assert client.get("/classes/1/runs/" + identifier, headers=principal).status_code == 200


def test_shutdown_requeues_only_unfinished_stage(client, teacher, ai_config, db_factory):
    identifier = queued(client, teacher)
    executor = Executor()
    asyncio.run(process_one(db_factory, executor))
    asyncio.run(process_one(db_factory, executor))

    async def interrupted():
        entered = asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.sleep(20)
        task = asyncio.create_task(process_one(db_factory, slow))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(interrupted())
    with db_factory() as db:
        job = db.get(AgentJob, identifier)
        assert job.status == "queued"
        assert job.lease_owner == ""
        assert set(job.state["outputs"]) == {"coordinator", "performance_analyst"}
    drain(db_factory, executor)
    assert Counter(name for name, _ in executor.calls)["performance_analyst"] == 1
    assert client.get("/classes/1/runs/" + identifier, headers=teacher).json()["status"] == "verified_metrics"


def test_independent_review_metrics_cannot_override_source(client, teacher, ai_config, db_factory):
    identifier = queued(client, teacher)
    async def dishonest(stage, *args, **kwargs):
        result = output(stage)
        if stage == "quality_reviewer":
            result["checked_mean_percentage"] = 0
        return result
    drain(db_factory, dishonest)
    run = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert run["status"] == "requires_review"
    assert "summary" not in run["result"]


def test_total_budget_stops_work_without_resetting_on_resume(client, teacher, ai_config, db_factory, monkeypatch):
    monkeypatch.setattr(ai_config, "ai_max_model_calls", 3)
    identifier = queued(client, teacher)
    executor = Executor()
    drain(db_factory, executor)
    run = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert run["status"] == "requires_review"
    assert run["result"]["error_code"] == "model_call_budget"
    assert len(executor.calls) == 3
    assert client.post("/classes/1/runs/" + identifier + "/resume", headers=teacher,
                       json={}).status_code == 409
