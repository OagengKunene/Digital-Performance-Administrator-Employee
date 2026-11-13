import asyncio
import os
import time
from pathlib import Path
import pytest
from sqlalchemy import select, func
from pae.db import (User, Mark, CaptureEvent, ReportNotification, AssessmentSource,
                    IngestionBatch, AgentJob, AgentRun, NotificationPreference,
                    EmailDelivery, MonitoringPolicy, FollowUp)
from pae.captures import queue_pending, recover_due
from pae.ingestion import scan_sources, process_batch
from pae.jobs import process_one
from pae.grounding import action_catalog, grounding_issues
from pae.notifications import process_email, usable_email, send_smtp

CSV = "learner_id,assessment,topic,earned,possible\nL4,Test 1,Algebra,15,20\n"


def source(client, teacher, **kwargs):
    result = client.post("/classes/1/sources", headers=teacher,
        json={"name": "Scheduled school export", **kwargs})
    assert result.status_code == 201, result.text
    return result.json()


def feed(client, source, content=CSV):
    return client.post(source["upload_endpoint"], content=content,
        headers={"X-Ingestion-Key": source["ingestion_key"], "Content-Type": "text/csv"})


def correct(client, teacher, db_factory, score=15):
    with db_factory() as db:
        identifier = db.scalar(select(Mark.id).where(Mark.learner_id == "L4"))
    result = client.patch(f"/classes/1/marks/{identifier}", headers=teacher,
        json={"earned": score, "reason": "Verified against the original marked script"})
    assert result.status_code == 200, result.text
    return result.json()


async def valid_stage(stage, data, goal, outputs, provider, model, request_id, on_request=None):
    return {
        "coordinator": {"rationale": "Investigate captured evidence and report limitations.",
                        "analyse_performance": True, "plan_support": True, "focus_topics": ["Recorded evidence"]},
        "performance_analyst": {"findings": ["Use the supplied source metrics; missing records are not zero."]},
        "intervention_planner": {"recommendations": action_catalog(data)},
        "reporting_agent": {"summary": "Evaluation of the captured learner-topic records, with source-supported actions.",
            "mean_percentage": data["mean_percentage"], "marked_count": data["marked_count"],
            "missing_count": data["missing_count"], "recommendations": action_catalog(data)},
        "quality_reviewer": {"approved": True, "issues": [],
            **{"checked_" + k: data[k] for k in ["mean_percentage", "marked_count", "missing_count", "assessment_count"]}},
    }[stage]


def finish(factory, executor=valid_stage):
    for _ in range(12):
        if not asyncio.run(process_one(factory, executor)):
            return
        with factory() as db:
            for job in db.scalars(select(AgentJob).where(AgentJob.status == "retrying")):
                job.next_at = 0
            db.commit()
    raise AssertionError("Evaluation did not stop")


def test_capture_delivers_without_prompt_policy_or_provider(client, teacher, principal, db_factory, monkeypatch):
    from pae.config import settings
    monkeypatch.setattr(settings(), "google_api_key", "")
    monkeypatch.setattr(settings(), "groq_api_key", "")
    result = correct(client, teacher, db_factory)
    assert result["capture_id"]
    assert not queue_pending(db_factory)
    for headers in [teacher, principal]:
        messages = client.get("/notifications", headers=headers).json()
        assert len(messages) == 1
        report = messages[0]["payload"]["report"]
        assert report["mean_percentage"] == 75
        assert report["marked_count"] == 4
        assert report["assessment_count"] == 1
        assert report["missing_count"] == 0
        assert messages[0]["in_app_status"] == "delivered"
        assert messages[0]["email_status"] == "not_requested"
    assert client.get("/notifications", headers={"Authorization": "Bearer other-token"}).json() == []
    assert client.get("/notifications", headers={"Authorization": "Bearer outsider-token"}).json() == []
    assert client.get("/classes/1/captures", headers=teacher).json()[0]["evaluation_status"] == "awaiting_provider"
    assert not correct(client, teacher, db_factory)["capture_id"]
    assert len(client.get("/notifications", headers=teacher).json()) == 1


def test_source_feed_captures_and_finishes_agent_delivery_automatically(client, teacher, principal, db_factory, ai_config):
    connected = source(client, teacher)
    assert feed(client, connected).status_code == 202
    assert process_batch(db_factory)
    assert queue_pending(db_factory)
    finish(db_factory)
    captures = client.get("/classes/1/captures", headers=teacher).json()
    assert captures[0]["evaluation_status"] == "verified_metrics", captures
    for headers in [teacher, principal]:
        messages = client.get("/notifications", headers=headers).json()
        assert {m["kind"] for m in messages} == {"capture_evaluation", "agent_evaluation"}
        report = next(m["payload"]["report"] for m in messages if m["kind"] == "agent_evaluation")
        assert report["review"]["checked_marked_count"] == 4
    assert feed(client, connected).json()["status"] == "completed"
    assert not process_batch(db_factory)
    # Same values with a different export layout are also harmless.
    assert feed(client, connected, CSV + "\n").status_code == 202
    assert process_batch(db_factory)
    assert len(client.get("/classes/1/captures", headers=teacher).json()) == 1


def test_each_capture_keeps_its_own_snapshot_while_later_marks_arrive(client, teacher, db_factory, ai_config):
    first = correct(client, teacher, db_factory, 10)["capture_id"]
    assert queue_pending(db_factory)
    second = correct(client, teacher, db_factory, 20)["capture_id"]
    finish(db_factory)
    assert queue_pending(db_factory)
    finish(db_factory)
    messages = client.get("/notifications", headers=teacher).json()
    reports = {m["payload"]["capture_id"]: m["payload"]["report"] for m in messages if m["kind"] == "agent_evaluation"}
    assert reports[first]["mean_percentage"] == 68.75
    assert reports[second]["mean_percentage"] == 81.25
    assert all("capture snapshot" in m["payload"]["scope"] for m in messages if m["kind"] == "agent_evaluation")


def test_watched_file_is_captured_once_and_symlink_is_ignored(client, teacher, db_factory, ai_config, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_config, "ingestion_inbox_root", str(tmp_path / "inbox"))
    monkeypatch.setattr(ai_config, "ingestion_settle_seconds", 0)
    connected = source(client, teacher)
    folder = Path(connected["inbox_folder"])
    scan_sources(db_factory)
    external = tmp_path / "private.csv"
    external.write_text(CSV)
    (folder / "link.csv").symlink_to(external)
    assert scan_sources(db_factory) == 0
    (folder / "marks.csv").write_text(CSV)
    assert scan_sources(db_factory) == 1
    assert process_batch(db_factory)
    assert not (folder / "marks.csv").exists()
    assert scan_sources(db_factory) == 0
    assert len(list((folder / "processed").glob("*.csv"))) == 1
    assert client.get("/classes/1/evidence", headers=teacher).json()["missing_count"] == 0


def test_conflict_rolls_back_all_rows_and_delivers_exception(client, teacher, db_factory):
    connected = source(client, teacher)
    conflict = CSV + "L1,Test 1,Algebra,19,20\n"
    assert feed(client, connected, conflict).status_code == 202
    assert process_batch(db_factory)
    with db_factory() as db:
        assert db.scalar(select(Mark).where(Mark.learner_id == "L4")).earned is None
        assert db.scalar(select(func.count()).select_from(CaptureEvent)) == 0
        assert db.scalar(select(IngestionBatch)).status == "requires_review"
        assert db.scalar(select(FollowUp)).status == "open"
    assert client.get("/notifications", headers=teacher).json()[0]["kind"] == "source_attention"
    # Monitoring must not close an unresolved source exception.
    client.put("/classes/1/monitoring", headers={"Authorization": "Bearer principal-token"},
               json={"enabled": True, "use_ai": False})
    from pae.monitoring import check_due
    check_due(db_factory)
    with db_factory() as db:
        assert db.scalar(select(FollowUp).where(FollowUp.issue_key.like("ingestion:%"))).status == "open"


def test_source_correction_authority_and_blank_never_erases_marks(client, teacher, db_factory):
    connected = source(client, teacher, allow_corrections=True)
    assert feed(client, connected, "learner_id,assessment,topic,earned,possible\nL1,Test 1,Algebra,19,20\n").status_code == 202
    process_batch(db_factory)
    assert feed(client, connected, "learner_id,assessment,topic,earned,possible\nL1,Test 1,Algebra,,20\n").status_code == 202
    process_batch(db_factory)
    with db_factory() as db:
        assert db.scalar(select(Mark).where(Mark.learner_id == "L1")).earned == 19


def test_missing_roster_evidence_is_retried_without_guessing(client, teacher, principal, db_factory):
    connected = source(client, teacher)
    assert feed(client, connected, CSV.replace("L4", "NEW")).status_code == 202
    process_batch(db_factory)
    with db_factory() as db:
        batch = db.scalar(select(IngestionBatch))
        assert batch.status == "waiting_evidence"
        batch.next_at = 0
        db.commit()
    assert client.post("/classes/1/learners", headers=principal,
                       json={"csv": "learner_id,name\nNEW,New learner\n"}).status_code == 200
    process_batch(db_factory)
    with db_factory() as db:
        assert db.scalar(select(IngestionBatch)).status == "completed"
        assert db.scalar(select(FollowUp).where(FollowUp.issue_key.like("ingestion:%"))).status == "resolved"


def test_feed_keys_are_scoped_rotatable_and_not_exposed(client, teacher, principal):
    connected = source(client, teacher)
    assert client.post(connected["upload_endpoint"], content=CSV, headers={"X-Ingestion-Key": "wrong"}).status_code == 401
    listing = client.get("/classes/1/sources", headers=teacher).json()
    assert "ingestion_key" not in listing[0] and "ingestion_token_hash" not in listing[0]
    assert client.get("/classes/1/sources", headers={"Authorization": "Bearer other-token"}).status_code == 404
    assert client.put("/classes/2/sources/" + connected["id"], headers=principal, json={"enabled": False}).status_code == 404
    rotated = client.post("/classes/1/sources/" + connected["id"] + "/rotate-key", headers=teacher).json()
    assert feed(client, connected).status_code == 401
    connected["ingestion_key"] = rotated["ingestion_key"]
    assert feed(client, connected).status_code == 202


def test_bad_recommendation_is_repaired_before_agent_publication(client, teacher, db_factory, ai_config):
    correct(client, teacher, db_factory)
    queue_pending(db_factory)
    calls = []
    async def mistake(stage, *args, **kwargs):
        calls.append(stage)
        result = await valid_stage(stage, *args, **kwargs)
        if stage == "reporting_agent" and calls.count(stage) == 1:
            result["recommendations"] = ["Give the learner a make-up test because their mark is missing."]
        return result
    finish(db_factory, mistake)
    assert calls.count("reporting_agent") == 2
    messages = client.get("/notifications", headers=teacher).json()
    assert any(m["kind"] == "agent_evaluation" for m in messages)
    assert "make-up" not in str(messages)


def test_transient_provider_failure_recovers_on_its_own(client, teacher, db_factory, ai_config):
    correct(client, teacher, db_factory)
    queue_pending(db_factory)
    calls = []
    async def unavailable(stage, *args, **kwargs):
        calls.append(stage)
        error = RuntimeError("private data")
        error.status_code = 503
        raise error
    finish(db_factory, unavailable)
    with db_factory() as db:
        job = db.scalar(select(AgentJob))
        assert job.status == "failed"
        assert job.state["request_attempts"] == 3
        job.next_at = 0
        db.commit()
    assert recover_due(db_factory)
    finish(db_factory)
    with db_factory() as db:
        assert db.scalar(select(AgentRun)).status == "verified_metrics"
        assert db.scalar(select(AgentJob)).state["request_attempts"] == 8


def test_inbox_read_ownership_and_email_preference_validation(client, teacher, principal, db_factory):
    correct(client, teacher, db_factory)
    message = client.get("/notifications", headers=teacher).json()[0]
    assert client.post("/notifications/" + message["id"] + "/read", headers=principal).status_code == 404
    assert client.post("/notifications/" + message["id"] + "/read", headers=teacher).status_code == 200
    assert client.put("/notification-preferences", headers=teacher, json={"email_enabled": True}).status_code == 422
    with db_factory() as db:
        db.get(User, 1).email = "teacher@school.co.za"
        db.commit()
    assert client.put("/notification-preferences", headers=teacher, json={"email_enabled": True}).status_code == 200
    assert client.put("/staff/4/notification-preferences", headers=principal, json={"email_enabled": False}).status_code == 404


def email_capture(client, teacher, db_factory, config, monkeypatch):
    for name, value in {"email_delivery_enabled": True, "smtp_host": "mail.school.co.za", "smtp_from": "reports@school.co.za"}.items():
        monkeypatch.setattr(config, name, value)
    with db_factory() as db:
        db.get(User, 1).email = "teacher@school.co.za"
        db.add(NotificationPreference(user_id=1, email_enabled=True))
        db.commit()
    correct(client, teacher, db_factory)


def test_email_outbox_retries_and_records_delivery(client, teacher, db_factory, ai_config, monkeypatch):
    email_capture(client, teacher, db_factory, ai_config, monkeypatch)
    sent = []
    def failure(*args):
        raise OSError("private SMTP credentials")
    assert process_email(db_factory, sender=failure)
    with db_factory() as db:
        delivery = db.scalar(select(EmailDelivery))
        assert delivery.status == "retrying" and delivery.attempts == 1
        assert delivery.last_error == "OSError"
        delivery.next_at = 0
        db.commit()
    assert process_email(db_factory, sender=lambda item, recipient: sent.append((item.id, recipient.email)))
    assert sent[0][1] == "teacher@school.co.za"
    assert not process_email(db_factory, sender=lambda *args: pytest.fail("Duplicate send"))
    assert client.get("/notifications", headers=teacher).json()[0]["email_status"] == "sent"


def test_email_rechecks_current_staff_access(client, teacher, db_factory, ai_config, monkeypatch):
    email_capture(client, teacher, db_factory, ai_config, monkeypatch)
    with db_factory() as db:
        db.get(User, 1).school = "No longer authorised"
        db.commit()
    assert process_email(db_factory, sender=lambda *args: pytest.fail("Revoked staff received a report"))
    with db_factory() as db:
        assert db.scalar(select(EmailDelivery)).status == "skipped"


def test_smtp_transport_requires_tls_and_stable_message_id(client, teacher, db_factory, ai_config, monkeypatch):
    email_capture(client, teacher, db_factory, ai_config, monkeypatch)
    calls = []
    class SMTP:
        def __init__(self, *args, **kwargs): calls.append("connect")
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): calls.append("ehlo")
        def starttls(self, *, context):
            assert context.verify_mode
            calls.append("tls")
        def send_message(self, message):
            assert "@school.co.za>" in message["Message-ID"]
            calls.append("send")
    monkeypatch.setattr("pae.notifications.smtplib.SMTP", SMTP)
    assert process_email(db_factory)
    assert calls == ["connect", "ehlo", "tls", "ehlo", "send"]


@pytest.mark.parametrize("value", ["teacher@example.test", "teacher@example.com", "bad", "a@@school.co.za", "a@school..co.za", "a@school.co.za\nBcc:x@y.za"])
def test_reserved_or_unsafe_email_addresses_are_not_sent(value):
    assert not usable_email(value)


def test_known_unsupported_decisions_are_detected():
    data = {"missing_count": 1, "marked_count": 3, "assessment_count": 1,
        "support_threshold": 50, "learners": [{"mean_percentage": 60, "support_review": False}]}
    assert grounding_issues("coordinator", {"focus_topics": ["Support Threshold Breach"]}, data)
    assert grounding_issues("reporting_agent", {"summary": "3 assessments marked", "recommendations": action_catalog(data)}, data)
    assert not any("targeted support" in a for a in action_catalog(data))


def test_capture_automatically_assigns_and_closes_evidence_followups(client, teacher, db_factory):
    correct(client, teacher, db_factory, 2)
    tasks = client.get("/classes/1/follow-ups", headers=teacher).json()
    assert any(t["issue_key"] == "support:L4" and t["status"] == "open" and t["assigned_to"] == 1 for t in tasks)
    correct(client, teacher, db_factory, 15)
    tasks = client.get("/classes/1/follow-ups", headers=teacher).json()
    assert next(t for t in tasks if t["issue_key"] == "support:L4")["status"] == "resolved"


def test_capture_control_loop_continues_while_a_model_is_waiting(monkeypatch, ai_config):
    from pae import worker
    observed = []
    entered = []
    def control(identifier):
        observed.append(identifier)
    async def waiting_model(factory):
        entered.append(True)
        await asyncio.sleep(20)
    monkeypatch.setattr(worker, "control_cycle", control)
    monkeypatch.setattr(worker, "process_one", waiting_model)
    monkeypatch.setattr(ai_config, "worker_poll_seconds", 0.02)
    async def exercise():
        task = asyncio.create_task(worker.serve())
        try:
            for _ in range(50):
                if len(observed) >= 3:
                    break
                await asyncio.sleep(0.01)
            assert entered and len(observed) >= 3
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(exercise())
