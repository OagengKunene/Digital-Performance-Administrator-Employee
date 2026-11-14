"""Visible real actions and bounded correction, rather than a scripted animation."""
import asyncio
import copy
import hashlib
import shutil

import pytest
from sqlalchemy import select, func

from pae.db import ScriptPhoto, Mark, ReportNotification, AgentJob
from pae.jobs import process_one
from pae.script_photos import process_photo
from test_script_photos import reading, upload, detail, drain


def test_class_agents_show_plans_actions_checks_and_actual_repair(client, teacher, db_factory, monkeypatch):
    from pae.config import settings
    monkeypatch.setattr(settings(), "ai_provider", "offline")
    response = client.post("/classes/1/runs", headers=teacher, json={"goal": "Review this class and verify its report.",
        "provider": "offline", "demonstrate_repair": True})
    assert response.status_code == 202
    for _ in range(12):
        if not asyncio.run(process_one(db_factory)): break
    result = client.get("/classes/1/runs/" + response.json()["id"], headers=teacher).json()
    assert result["status"] == "verified_metrics"
    activity = result["activity"]
    for agent in ("coordinator", "performance_analyst", "intervention_planner", "reporting_agent", "quality_reviewer"):
        assert {e["phase"] for e in activity if e["agent"] == agent} >= {"plan", "act", "observe", "check"}
    repair = next(i for i, e in enumerate(activity) if e["phase"] == "correct")
    approval = next(i for i, e in enumerate(activity) if e["phase"] == "complete")
    assert repair < approval
    assert any(e["evidence"].get("rejected_report") for e in activity)
    assert result["progress"]["repair_count"] == 1


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Actual OCR requires Tesseract")
def test_controlled_demo_catches_fault_then_re_reads_real_source(client, teacher, db_factory, monkeypatch):
    from pae import script_photos
    rereads = []
    original = script_photos.reread_script
    def observed(image, reader, strategy, feedback):
        rereads.append((hashlib.sha256(image).hexdigest(), strategy, feedback))
        return original(image, reader, strategy, feedback)
    monkeypatch.setattr(script_photos, "reread_script", observed)
    response = client.post("/classes/1/colleague-demo?reader=local", headers=teacher)
    assert response.status_code == 202, response.text
    identifier = response.json()["photo"]["id"]
    for _ in range(12):
        if not process_photo(db_factory): break
    item = detail(client, teacher, identifier)
    assert item["status"] == "completed", item["issues"]
    assert item["demonstrate_repair"] is True and 1 <= item["repair_count"] <= 2
    history = item["report"]["workflow"]["readings"]
    assert history[0]["extraction"]["questions"][2]["earned"] == 8
    assert history[-1]["extraction"]["questions"][2]["earned"] == 3
    assert history[0]["validation_issues"] and not history[-1]["validation_issues"]
    assert len(rereads) == item["repair_count"]
    assert item["report"]["review"]["checked_percentage"] == 62.5
    with db_factory() as db:
        assert rereads[0][0] == db.get(ScriptPhoto, identifier).image_hash
        assert db.get(Mark, 4).earned is None
    phases = {(e["agent"], e["phase"]) for e in item["activity"]}
    assert phases >= {("coordinator", "plan"), ("correction_agent", "correct"), ("correction_agent", "act"),
                      ("correction_agent", "check"), ("quality_reviewer", "check"), ("delivery", "complete")}


def test_natural_bad_read_is_replaced_only_by_a_new_source_read(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    wrong = copy.deepcopy(reading)
    wrong["questions"][2]["earned"] = 8
    calls = []
    def reader(*args):
        calls.append(True)
        return wrong if len(calls) == 1 else reading
    for _ in range(12):
        if not process_photo(db_factory, reader): break
    item = detail(client, teacher, identifier)
    assert len(calls) == 2 and item["status"] == "completed"
    assert item["demonstrate_repair"] is False
    assert item["original_extraction"]["questions"][2]["earned"] == 8
    assert item["report"]["questions"][2]["earned"] == 3


def test_unchanged_wrong_source_stops_at_budget_without_guessed_marks(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    reading["total_earned"] = 39
    drain(db_factory, reading)
    item = detail(client, teacher, identifier)
    assert item["status"] == "requires_review"
    assert item["attempts"] == 3 and item["repair_count"] == 2
    assert item["extraction"]["total_earned"] == 39
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(ReportNotification)) == 0


def test_repeated_bad_report_repairs_remain_bounded(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    for _ in range(15):
        with db_factory() as db:
            item = db.get(ScriptPhoto, identifier)
            if item.status == "reviewing":
                item.report = {**item.report, "mean_percentage": 99}
                db.commit()
        if not process_photo(db_factory, lambda *args: reading): break
    item = detail(client, teacher, identifier)
    assert item["status"] == "requires_review" and item["report_repairs"] == 2
    assert client.get("/notifications", headers=teacher).json() == []


def test_activity_catalog_is_class_scoped(client, teacher, principal, db_factory):
    response = client.post("/classes/1/colleague-demo?reader=local", headers=teacher)
    assert response.status_code == 202
    jobs = client.get("/classes/1/agent-activity", headers=teacher).json()
    assert {job["kind"] for job in jobs} == {"script_photo", "class_report"}
    assert client.get("/classes/1/agent-activity", headers={"Authorization": "Bearer other-token"}).status_code == 404
    assert client.get("/classes/1/agent-activity", headers={"Authorization": "Bearer outsider-token"}).status_code == 404
    # Repeated demonstrations reuse the active class job; photo runs stay distinct.
    second = client.post("/classes/1/colleague-demo?reader=local", headers=teacher).json()
    assert second["class_report_reused"] is True
    assert second["photo"]["id"] != response.json()["photo"]["id"]


def test_fractional_marks_and_rounding_match_the_independent_reviewer(client, teacher, db_factory):
    from pae.script_reader import ScriptReading
    result = ScriptReading(total_earned=0.3, total_possible=1,
        questions=[{"question": "Q1", "topic": "Algebra", "earned": 0.1, "possible": 0.5},
                   {"question": "Q2", "topic": "Algebra", "earned": 0.2, "possible": 0.5}]).model_dump()
    identifier = upload(client, teacher)
    drain(db_factory, result)
    item = detail(client, teacher, identifier)
    assert item["status"] == "completed"
    assert item["report"]["topics"][0]["earned"] == 0.3
    assert item["report"]["review"]["checked_percentage"] == 30


def test_correction_cannot_bypass_a_written_total_by_dropping_it(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    bad = copy.deepcopy(reading)
    bad["questions"][2]["earned"] = 8
    attempts = []
    def read(*args):
        attempts.append(True)
        candidate = copy.deepcopy(bad)
        if len(attempts) > 1:
            candidate["total_earned"] = candidate["total_possible"] = None
        return candidate
    for _ in range(10):
        if not process_photo(db_factory, read): break
    item = detail(client, teacher, identifier)
    assert item["status"] == "requires_review" and item["attempts"] == 3
    assert any("lost a previously visible" in issue for issue in item["issues"])
    assert client.get("/notifications", headers=teacher).json() == []


def test_changed_total_needs_a_second_matching_source_read(client, teacher, db_factory, reading):
    identifier = upload(client, teacher)
    attempts = []
    def read(*args):
        attempts.append(True)
        candidate = copy.deepcopy(reading)
        if len(attempts) == 1:
            candidate["total_earned"] = 35
        return candidate
    for _ in range(12):
        if not process_photo(db_factory, read): break
    item = detail(client, teacher, identifier)
    assert item["status"] == "completed" and item["attempts"] == 3
    assert item["report"]["review"]["checked_percentage"] == 62.5


def test_controlled_demo_cannot_claim_correction_when_no_fault_was_inserted(client, teacher, db_factory):
    from pae.script_reader import ScriptReading
    response = client.post("/classes/1/colleague-demo?reader=local", headers=teacher)
    assert response.status_code == 202
    identifier = response.json()["photo"]["id"]
    total_only = ScriptReading(total_earned=25, total_possible=40).model_dump()
    for _ in range(12):
        if not process_photo(db_factory, lambda *args: total_only): break
    item = detail(client, teacher, identifier)
    assert item["status"] == "requires_review"
    assert not item["report"]["workflow"].get("fault_injected")
    assert client.get("/notifications", headers=teacher).json() == []
