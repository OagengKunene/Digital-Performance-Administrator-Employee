"""Durable report jobs, stage checkpoints and fenced worker leases."""
import copy
import asyncio
import hashlib
import json
import time
import uuid
from sqlalchemy import select, update
from .db import AgentRun, AgentJob, User, MonitoringPolicy, utcnow
from .services import allowed_class, evidence, audit
from .config import settings
from .providers import provider_details
from .agents import WorkPlan, ReviewVerdict
from .activity import activity, public_activity, PLANS, output_summary

ACTIVE = {"queued", "running", "retrying"}


class LeaseLost(Exception):
    pass


def fingerprint(data):
    value = copy.deepcopy(data)
    for key in ["learners", "topics", "interventions"]:
        value[key] = sorted(value.get(key, []), key=lambda item: json.dumps(item, sort_keys=True))
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def baseline_report(data):
    return {"kind": "deterministic_statistics", "mean_percentage": data["mean_percentage"],
            "marked_count": data["marked_count"], "missing_count": data["missing_count"],
            "attendance_recorded_status_counts": data.get("attendance_recorded_status_counts", {}),
            "definitions": data.get("definitions", {}), "ai_interpretation": "Not included"}


def enqueue(db, user, class_id, goal, *, trigger="manual", dedupe_key=None, snapshot=None,
            provider=None, demonstrate_repair=False):
    allowed_class(db, user, class_id)
    data = copy.deepcopy(snapshot) if snapshot is not None else evidence(db, user, class_id)
    if data.get("class_id") != class_id:
        raise ValueError("Snapshot class mismatch")
    provider, model, _ = provider_details(provider)
    if dedupe_key:
        existing = db.scalar(select(AgentJob).where(AgentJob.dedupe_key == dedupe_key))
        if existing:
            return db.get(AgentRun, existing.id)
    identifier = str(uuid.uuid4())
    run = AgentRun(id=identifier, user_id=user.id, class_id=class_id, goal=goal,
                   status="queued", result={"message": "Queued for the worker.",
                   "statistical_report": baseline_report(data)}, events=[])
    db.add(run)
    db.flush()
    db.add(AgentJob(id=identifier, provider=provider, model=model,
        active_class_id=class_id,
        fallback=settings().ai_fallback_provider, snapshot=data, dedupe_key=dedupe_key,
        state={"outputs": {}, "attempts": {}, "request_attempts": 0, "repair_count": 0,
               "request_budget": settings().ai_max_model_calls, "trigger": trigger,
               "source_hash": fingerprint(data), "active_stage": "coordinator",
               "demonstrate_repair": bool(demonstrate_repair)}))
    db.flush()
    audit(db, user, class_id, "agent_job_queued", {"run_id": identifier, "trigger": trigger,
                                                "provider": provider, "model": model,
                                                "demonstrate_repair": bool(demonstrate_repair)})
    return run


def next_stage(state):
    outputs = state["outputs"]
    if "coordinator" not in outputs:
        return "coordinator"
    plan = WorkPlan.model_validate(outputs["coordinator"])
    if plan.analyse_performance and "performance_analyst" not in outputs:
        return "performance_analyst"
    if plan.plan_support and "intervention_planner" not in outputs:
        return "intervention_planner"
    for name in ["reporting_agent", "quality_reviewer"]:
        if name not in outputs:
            return name
    return None


def public_run(db, run):
    result = {"id": run.id, "at": run.at, "status": run.status, "goal": run.goal,
              "result": run.result, "events": run.events, "activity": public_activity(run.events)}
    job = db.get(AgentJob, run.id)
    if job:
        state = job.state
        result["progress"] = {"active_stage": state.get("active_stage"),
            "completed_stages": [name for name in state.get("outputs", {}) if name != "review_feedback"],
            "request_attempts": state.get("request_attempts", 0),
            "request_budget": state.get("request_budget"),
            "next_attempt_at": job.next_at, "provider": job.provider, "model": job.model,
            "trigger": state.get("trigger"), "updated_at": job.updated_at,
            "demonstrate_repair": state.get("demonstrate_repair", False),
            "repair_count": state.get("repair_count", 0),
            "completed_outputs": state.get("outputs", {}),
            "resumable": job.status == "failed" and
                state.get("request_attempts", 0) < state.get("request_budget", 0)}
    return result


def claim(factory, now=None):
    now = time.time() if now is None else now
    owner = str(uuid.uuid4())
    with factory() as db:
        candidates = db.scalars(select(AgentJob.id).where(
            AgentJob.status.in_(ACTIVE), AgentJob.next_at <= now,
            AgentJob.lease_until <= now).order_by(AgentJob.next_at, AgentJob.id).limit(10)).all()
        for identifier in candidates:
            changed = db.execute(update(AgentJob).where(AgentJob.id == identifier,
                AgentJob.status.in_(ACTIVE), AgentJob.next_at <= now,
                AgentJob.lease_until <= now).values(status="running", lease_owner=owner,
                lease_until=now + settings().ai_timeout_seconds + 60, updated_at=utcnow()))
            if changed.rowcount:
                run = db.get(AgentRun, identifier)
                run.status = "running"
                db.commit()
                return identifier, owner
            db.rollback()
    return None


def checkpoint_event(factory, identifier, owner, event):
    with factory() as db:
        changed = db.execute(update(AgentJob).where(AgentJob.id == identifier,
            AgentJob.lease_owner == owner, AgentJob.lease_until > time.time()).values(updated_at=utcnow()))
        if not changed.rowcount:
            raise LeaseLost()
        job = db.get(AgentJob, identifier)
        run = db.get(AgentRun, identifier)
        user = db.get(User, run.user_id)
        if not user:
            raise LeaseLost()
        allowed_class(db, user, run.class_id)
        if job.state.get("trigger") == "monitor":
            policy = db.get(MonitoringPolicy, run.class_id)
            if not policy or not policy.enabled or not policy.use_ai:
                raise ValueError("Automatic AI monitoring was disabled")
        run.events = [*run.events, {**event, "at": utcnow()}]
        db.commit()


def save(factory, identifier, owner, state, status, result, *, event, next_at=0):
    with factory() as db:
        run = db.get(AgentRun, identifier)
        changed = db.execute(update(AgentJob).where(AgentJob.id == identifier,
            AgentJob.lease_owner == owner, AgentJob.lease_until > time.time()).values(
            state=state, status=status, lease_owner="", lease_until=0,
            next_at=next_at, updated_at=utcnow()))
        if not changed.rowcount:
            db.rollback()
            raise LeaseLost()
        db.get(AgentJob, identifier).active_class_id = run.class_id if status in ACTIVE else None
        run.status, run.result = status, result
        run.events = [*run.events, {**event, "at": utcnow()}]
        from .captures import job_updated
        job_updated(db, run, db.get(AgentJob, identifier))
        if status == "verified_metrics":
            run.events = [*run.events, activity("delivery", "complete", "Reviewed report delivered to assigned teacher and principal inboxes.", tool="publish")]
        db.commit()


def resume(db, run, provider=None):
    job = db.get(AgentJob, run.id)
    if not job or job.status != "failed":
        raise ValueError("Only failed checkpointed jobs can be resumed")
    state = copy.deepcopy(job.state)
    if state["request_attempts"] >= state["request_budget"]:
        raise ValueError("The report request budget is exhausted")
    name, model = job.provider, job.model
    if provider:
        name, model, _ = provider_details(provider)
    state["attempts"] = {}
    state.pop("stage_provider", None)
    changed = db.execute(update(AgentJob).where(AgentJob.id == run.id,
        AgentJob.status == "failed").values(state=state, status="queued", next_at=0,
        active_class_id=run.class_id, provider=name, model=model,
        lease_owner="", lease_until=0, updated_at=utcnow()))
    if not changed.rowcount:
        raise ValueError("This job was already resumed or changed. Refresh its progress.")
    run.status = "queued"
    run.result = {"message": "Resuming at the first unfinished stage.",
                  "statistical_report": baseline_report(job.snapshot)}


async def process_one(factory, executor=None):
    from .agents import run_stage, StageOutputInvalid, ReviewedReport, SCHEMAS
    from .grounding import grounding_issues
    from .ai_diagnostics import describe_ai_error
    from .providers import ProviderNotConfigured
    claimed = claim(factory)
    if not claimed:
        return False
    identifier, owner = claimed
    with factory() as db:
        job, run = db.get(AgentJob, identifier), db.get(AgentRun, identifier)
        state, data, goal = copy.deepcopy(job.state), copy.deepcopy(job.snapshot), run.goal
        provider, model = job.provider, job.model
        fallback = job.fallback
        if state.get("trigger") == "monitor":
            policy = db.get(MonitoringPolicy, run.class_id)
            if not policy or not policy.enabled or not policy.use_ai:
                save(factory, identifier, owner, state, "cancelled",
                     {"message": "Automatic AI monitoring was disabled."},
                     event={"type": "automation_disabled"})
                return True
        try:
            user = db.get(User, run.user_id)
            if not user:
                raise ValueError()
            allowed_class(db, user, run.class_id)
        except Exception:
            save(factory, identifier, owner, state, "failed",
                 {"message": "Staff class authorisation changed.", "error_code": "class_access"},
                 event={"type": "authorisation_blocked"})
            return True
    stage = next_stage(state)
    if state["request_attempts"] >= state["request_budget"]:
        save(factory, identifier, owner, state, "requires_review",
             {"message": "Report request budget exhausted.", "error_code": "model_call_budget",
              "statistical_report": baseline_report(data)}, event={"type": "budget_blocked"})
        return True
    chosen_provider = state.get("stage_provider", provider)
    if chosen_provider != provider:
        _, model, _ = provider_details(chosen_provider)
    state["active_stage"] = stage
    state["request_attempts"] += 1
    state["attempts"][stage] = state["attempts"].get(stage, 0) + 1
    # Persist the dispatched attempt before inference; a crash cannot erase usage.
    with factory() as db:
        changed = db.execute(update(AgentJob).where(AgentJob.id == identifier,
            AgentJob.lease_owner == owner, AgentJob.lease_until > time.time()).values(state=state))
        if not changed.rowcount:
            raise LeaseLost()
        run = db.get(AgentRun, identifier)
        run.events = [*run.events,
            activity(stage, "plan", PLANS[stage], evidence={"attempt": state["attempts"][stage],
                "review_feedback": state["outputs"].get("review_feedback", {}), "source_hash": state["source_hash"]}),
            activity(stage, "act", "Execute the " + stage.replace("_", " ") + " stage.",
                tool="offline_stage" if chosen_provider == "offline" else "ADK run_stage",
                evidence={"provider": chosen_provider, "model": model}),
            {"type": "stage_started", "agent": stage,
            "provider": chosen_provider, "model": model, "at": utcnow()}]
        db.commit()

    async def on_request(event):
        checkpoint_event(factory, identifier, owner, event)

    runner = executor or run_stage
    if executor is None and chosen_provider == "offline":
        from functools import partial
        from .offline_engine import offline_stage
        runner = partial(offline_stage, demonstrate_repair=state.get("demonstrate_repair", False))
    try:
        result = await runner(stage, data, goal, state["outputs"],
            chosen_provider, model, str(uuid.uuid4()), on_request=on_request)
        try:
            result = SCHEMAS[stage].model_validate(result).model_dump()
        except ValueError:
            raise StageOutputInvalid() from None
        if stage == "reporting_agent":
            from .agents import review_numbers
            numerical_issues = review_numbers(ReviewedReport.model_validate(result), data)
            if numerical_issues:
                raise StageOutputInvalid(numerical_issues)
        issues = grounding_issues(stage, result, data)
        if issues:
            raise StageOutputInvalid(issues)
        checkpoint_event(factory, identifier, owner, activity(stage, "observe", "Structured output received.",
            evidence=output_summary(stage, result)))
        checkpoint_event(factory, identifier, owner, activity(stage, "check", "Stage schema and applicable source-grounding checks passed.",
            tool="schema and grounding checks"))
        state["outputs"][stage] = result
        state.pop("stage_provider", None)
        status, response, event_type = "queued", {"message": "Stage saved."}, "stage_completed"
        if stage == "quality_reviewer":
            verdict = ReviewVerdict.model_validate(result)
            issues = list(verdict.issues)
            for field in ["mean_percentage", "marked_count", "missing_count"] + (["assessment_count"] if "assessment_count" in data else []):
                if getattr(verdict, "checked_" + field) != data[field]:
                    issues.append("Independent review metrics do not match source: " + field)
            report = ReviewedReport.model_validate(state["outputs"]["reporting_agent"])
            from .agents import review_numbers
            issues.extend(review_numbers(report, data))
            if not verdict.approved or issues:
                if state["repair_count"] < 2:
                    checkpoint_event(factory, identifier, owner, activity("quality_reviewer", "check",
                        "Report rejected; publication is blocked until repair and re-review.",
                        evidence={"issues": issues or ["Reviewer did not approve"], "rejected_report": output_summary("reporting_agent", state["outputs"]["reporting_agent"])}))
                    checkpoint_event(factory, identifier, owner, activity("reporting_agent", "correct",
                        "Revise the rejected report using the reviewer's specific feedback, then request another review.",
                        tool="reporting_agent repair", evidence={"issues": issues or ["Reviewer did not approve"]}))
                    state["repair_count"] += 1
                    state["outputs"]["review_feedback"] = {"issues": issues or ["Reviewer did not approve"]}
                    state["outputs"].pop("reporting_agent", None)
                    state["outputs"].pop("quality_reviewer", None)
                    response, event_type = {"message": "Reviewer requested a repair."}, "repair_requested"
                else:
                    status = "requires_review"
                    response = {"message": "Review blocked publication after two repairs.",
                                "issues": issues, "statistical_report": baseline_report(data)}
            else:
                with factory() as db:
                    run = db.get(AgentRun, identifier)
                    user = db.get(User, run.user_id)
                    current = evidence(db, user, run.class_id)
                    policy = db.get(MonitoringPolicy, run.class_id)
                    automation_stopped = state.get("trigger") == "monitor" and (
                        not policy or not policy.enabled or not policy.use_ai)
                if automation_stopped:
                    status, response = "cancelled", {"message": "Automatic AI monitoring was disabled."}
                elif state.get("trigger") != "capture" and fingerprint(current) != state["source_hash"]:
                    status = "requires_review"
                    response = {"message": "Class records changed after this snapshot; generate a new report.",
                                "error_code": "snapshot_changed",
                                "statistical_report": baseline_report(data)}
                else:
                    status = "verified_metrics"
                    response = {**report.model_dump(), "assessment_count": data.get("assessment_count"),
                                "review": verdict.model_dump(),
                                "source_hash": state["source_hash"]}
                    checkpoint_event(factory, identifier, owner, activity("quality_reviewer", "complete",
                        "Report approved and source metrics verified.", evidence=output_summary("quality_reviewer", result)))
        state["active_stage"] = next_stage(state) if status == "queued" else None
        if status != "verified_metrics":
            response.setdefault("statistical_report", baseline_report(data))
        save(factory, identifier, owner, state, status, response,
             event={"type": event_type, "agent": stage})
    except asyncio.CancelledError:
        try:
            save(factory, identifier, owner, state, "queued",
                 {"message": "Worker stopped; the unfinished stage is queued for recovery.",
                  "statistical_report": baseline_report(data)},
                 event={"type": "worker_interrupted", "agent": stage})
        except LeaseLost:
            pass
        raise
    except LeaseLost:
        return True
    except Exception as error:
        if isinstance(error, ProviderNotConfigured):
            detail = {"message": "Configure the selected provider's Codespaces secret.",
                      "error_code": "provider_credentials", "provider_code": None}
        elif isinstance(error, StageOutputInvalid):
            detail = {"message": "Stage output failed schema or numerical validation.",
                      "error_code": "stage_output_invalid", "provider_code": None}
            state["outputs"]["review_feedback"] = {"issues": error.issues}
        else:
            detail = describe_ai_error(error)
        count = state["attempts"][stage]
        retryable = detail.get("provider_code") in {500, 502, 503, 504} or detail["error_code"] in {
            "ai_timeout", "provider_connection", "stage_output_invalid"}
        switched = bool(fallback and fallback != chosen_provider and
                        detail["error_code"].startswith("provider_"))
        can_retry = state["request_attempts"] < state["request_budget"] and count < settings().ai_retry_attempts
        if switched and can_retry:
            state["stage_provider"] = fallback
            status = "retrying"
        elif retryable and can_retry:
            status = "retrying"
        else:
            status = "failed"
        checkpoint_event(factory, identifier, owner, activity(stage,
            ("correct" if isinstance(error, StageOutputInvalid) else "retry") if status == "retrying" else "blocked",
            "Stage failed validation or provider execution; retry only the unfinished work." if status == "retrying"
            else "Stage stopped within its attempt budget; completed work remains saved.",
            evidence={"error_code": detail["error_code"],
                "issues": error.issues if isinstance(error, StageOutputInvalid) else [],
                "retry_scheduled": status == "retrying", "next_provider": fallback if switched and can_retry else chosen_provider}))
        delay = min(120, settings().ai_retry_delay_seconds * 2 ** (count - 1))
        detail["statistical_report"] = baseline_report(data)
        save(factory, identifier, owner, state, status, detail,
             event={"type": "stage_failed", "agent": stage, "error_code": detail["error_code"],
                    "provider_code": detail.get("provider_code")},
             next_at=time.time() + (delay if status == "retrying" else settings().capture_retry_seconds)
                     if status in {"retrying", "failed"} else 0)
    return True
