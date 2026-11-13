"""Transactional mark-capture triggers: evaluate, route and queue without a prompt."""
import copy
import uuid
import time
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import CaptureEvent, AgentRun, AgentJob, User, MonitoringPolicy, utcnow
from .services import evidence, allowed_class, audit
from .grounding import action_catalog
from .notifications import publish
from .providers import configured
from .config import settings


def statistical_evaluation(data):
    flagged = sum(bool(row.get("support_review")) for row in data["learners"])
    assessment_count = data.get("assessment_count", 0)
    return {"kind": "verified_data_evaluation", "ai_interpretation": "Not included",
        "class_name": data["class_name"], "subject": data["subject"],
        "summary": (f"{data['class_name']} has {data['marked_count']} marked learner-topic records "
                    f"from {assessment_count} distinct assessment(s), with {data['missing_count']} missing topic record(s). "
                    f"{flagged} learner(s) are below the configured support threshold. "
                    "Missing marks are record gaps until the underlying evidence is verified."),
        "mean_percentage": data["mean_percentage"], "assessment_count": assessment_count,
        "marked_count": data["marked_count"], "missing_count": data["missing_count"],
        "below_support_threshold_count": flagged, "support_threshold": data["support_threshold"],
        "attendance_recorded_status_counts": data["attendance_recorded_status_counts"],
        "topics": data["topics"], "actions": action_catalog(data),
        "definitions": data["definitions"]}


def record_capture(db, user, class_id, kind, changed_records):
    if not changed_records:
        return None
    from .jobs import fingerprint
    db.flush()
    data = evidence(db, user, class_id)
    from .monitoring import sync_findings
    sync_findings(db, allowed_class(db, user, class_id), data, time.time())
    identifier, captured_at = str(uuid.uuid4()), utcnow()
    item = CaptureEvent(id=identifier, class_id=class_id, actor_id=user.id, kind=kind,
        changed_records=changed_records, snapshot=copy.deepcopy(data),
        source_hash=fingerprint(data), created_at=captured_at)
    db.add(item)
    db.flush()
    publish(db, class_id, "capture_evaluation", "capture:" + identifier,
        f"Performance evaluation: {data['class_name']} · new records",
        {"capture_id": identifier, "captured_at": captured_at,
         "scope": "Immutable snapshot at this capture; not necessarily the latest class state.",
         "source_hash": item.source_hash, "changed_records": changed_records,
         "report": statistical_evaluation(data), "agent_evaluation": "pending"})
    audit(db, user, class_id, "capture_report_delivered",
          {"capture_id": identifier, "changed_records": changed_records,
           "routing": "assigned_teacher_and_school_principals"})
    return identifier


def queue_pending(factory):
    from .jobs import enqueue, fingerprint
    with factory() as db:
        item = db.scalar(select(CaptureEvent).where(
            CaptureEvent.evaluation_status.in_(["pending", "awaiting_provider"]),
            CaptureEvent.class_id.not_in(select(AgentJob.active_class_id).where(AgentJob.active_class_id.is_not(None)))
            ).order_by(CaptureEvent.created_at, CaptureEvent.id)
            .with_for_update(skip_locked=True).limit(1))
        if not item:
            return False
        actor = db.get(User, item.actor_id)
        try:
            allowed_class(db, actor, item.class_id)
        except Exception:
            item.evaluation_status = "authorisation_blocked"
            db.commit()
            return True
        if not configured():
            item.evaluation_status = "awaiting_provider"
            db.commit()
            return False
        if db.scalar(select(AgentJob.id).where(AgentJob.active_class_id == item.class_id)):
            return False
        goal = (f"Independently evaluate capture {item.id}, recorded at {item.created_at}. "
                "This is an immutable capture snapshot, which may precede later captures. "
                "Decide the required investigation, verify your work, and prepare the result "
                "for the assigned teacher and school principal. Select only source-supported actions.")
        try:
            with db.begin_nested():
                run = enqueue(db, actor, item.class_id, goal, trigger="capture",
                    dedupe_key="capture:" + item.id, snapshot=item.snapshot)
                item.run_id, item.evaluation_status = run.id, "queued"
                policy = db.get(MonitoringPolicy, item.class_id)
                if policy:
                    policy.last_hash = fingerprint(item.snapshot)
        except IntegrityError:
            return False
        db.commit()
    return True


def job_updated(db, run, job):
    item = db.scalar(select(CaptureEvent).where(CaptureEvent.run_id == run.id))
    if item:
        item.evaluation_status = run.status
    if run.status == "verified_metrics":
        publish(db, run.class_id, "agent_evaluation", "agent:" + run.id,
            "Reviewed agent performance evaluation",
            {"run_id": run.id, "capture_id": item.id if item else None,
             "captured_at": item.created_at if item else run.at,
             "scope": "Reviewed evaluation of the saved capture snapshot." if item else "Reviewed class report.",
             "report": copy.deepcopy(run.result), "source_hash": job.state.get("source_hash")})
    elif item and run.status in {"failed", "requires_review"}:
        publish(db, run.class_id, "evaluation_attention", "attention:" + run.id,
            "Agent evaluation needs attention; data report was delivered",
            {"capture_id": item.id, "run_id": run.id,
             "scope": "Delivery of the factual capture report is independent of AI availability.",
             "report": {"summary": run.result.get("message", "Evaluation paused."),
                        "error_code": run.result.get("error_code")},
             "saved_stages": list(job.state.get("outputs", {}))})


def recover_due(factory):
    """Retry captured evaluations without a new prompt, within their original budget."""
    from .jobs import resume
    now = time.time()
    with factory() as db:
        candidates = db.scalars(select(CaptureEvent).where(
            CaptureEvent.evaluation_status == "failed").limit(20)).all()
        for item in candidates:
            run, job = db.get(AgentRun, item.run_id), db.get(AgentJob, item.run_id)
            if not run or not job or job.next_at > now:
                continue
            category = run.result.get("error_code")
            if category not in {"provider_unavailable", "provider_connection", "ai_timeout", "provider_quota", "provider_credentials"}:
                continue
            if job.state["request_attempts"] >= job.state["request_budget"]:
                continue
            actor = db.get(User, run.user_id)
            try:
                allowed_class(db, actor, run.class_id)
                if not configured(job.provider):
                    continue
                if db.scalar(select(AgentJob.id).where(AgentJob.active_class_id == run.class_id)):
                    continue
                resume(db, run)
                item.evaluation_status = "queued"
                db.commit()
                return True
            except (ValueError, IntegrityError):
                db.rollback()
    return False

