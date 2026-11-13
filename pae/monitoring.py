"""Policy-controlled autonomous observation, follow-up and AI goal creation."""
import time
from datetime import date, datetime, timezone
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from .db import MonitoringPolicy, FollowUp, AgentJob, SchoolClass, User, AutomaticReportBudget, utcnow
from .services import evidence, audit
from .jobs import fingerprint, enqueue
from .providers import configured
from .config import settings


def findings(data, today=None):
    today = today or date.today()
    desired = {}
    if data["missing_count"]:
        desired["missing_marks"] = {"kind": "missing_marks", "count": data["missing_count"],
                                   "action": "Check the missing assessment-topic records; establish the reason before interpreting them."}
    for learner in data["learners"]:
        if learner["support_review"]:
            desired["support:" + learner["learner_id"]] = {
                "kind": "learner_support", "learner_id": learner["learner_id"],
                "mean_percentage": learner["mean_percentage"],
                "action": "Review the recorded performance and agree appropriate support."}
    absent = data["attendance_recorded_status_counts"].get("absent", 0)
    if absent:
        desired["attendance_review"] = {"kind": "recorded_absences", "count": absent,
            "action": "Review supplied absence records; no cause or full-calendar attendance rate is inferred."}
    for item in data["interventions"]:
        if item["status"] != "completed" and date.fromisoformat(item["due"]) < today:
            desired["overdue:" + str(item["id"])] = {"kind": "overdue_intervention",
                "intervention_id": item["id"], "learner_id": item["learner_id"],
                "action": "Record support completion evidence or an updated follow-up plan."}
    return desired


def sync_findings(db, school_class, data, now):
    class_id = school_class.id
    desired = findings(data, datetime.fromtimestamp(now, timezone.utc).date())
    existing = {row.issue_key: row for row in db.scalars(
        select(FollowUp).where(FollowUp.class_id == class_id))}
    changed = []
    for key, detail in desired.items():
        row = existing.get(key)
        if not row:
            row = FollowUp(class_id=class_id, issue_key=key,
                assigned_to=school_class.teacher_id, detail=detail, due_at=now + 86400)
            db.add(row)
            changed.append(key)
        else:
            if row.status == "resolved" and row.detail != detail:
                row.status, row.due_at, row.escalated = "open", now + 86400, False
                row.outcome = ""
                changed.append(key)
            row.assigned_to, row.detail = school_class.teacher_id, detail
            if row.status != "resolved" and now > row.due_at and not row.escalated:
                row.escalated = True
                changed.append("escalated:" + key)
            row.updated_at = utcnow()
    for key, row in existing.items():
        if not key.startswith("ingestion:") and key not in desired and row.status != "resolved":
            row.status = "resolved"
            row.outcome = "Automatically closed because this issue no longer appears in current evidence."
            row.updated_at = utcnow()
            changed.append("closed:" + key)
    return changed


def check_due(factory, now=None):
    now = time.time() if now is None else now
    with factory() as db:
        policy = db.scalar(select(MonitoringPolicy).where(MonitoringPolicy.enabled.is_(True),
            MonitoringPolicy.next_at <= now).order_by(MonitoringPolicy.next_at)
            .with_for_update(skip_locked=True).limit(1))
        if not policy:
            return False
        user, school_class = db.get(User, policy.owner_id), db.get(SchoolClass, policy.class_id)
        if not user or user.role != "principal" or not school_class or user.school != school_class.school:
            policy.enabled = False
            db.commit()
            return True
        data = evidence(db, user, policy.class_id)
        current_hash = fingerprint(data)
        changed = sync_findings(db, school_class, data, now)
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if not db.get(AutomaticReportBudget, day):
            try:
                with db.begin_nested():
                    db.add(AutomaticReportBudget(day=day, used=0))
                    db.flush()
            except IntegrityError:
                pass
        per_class = db.scalar(select(func.count()).select_from(AgentJob).where(
            AgentJob.dedupe_key.like(f"monitor:{policy.class_id}:{day}:%")))
        queued = None
        if (policy.use_ai and current_hash != policy.last_hash and configured()
                and per_class < policy.daily_ai_limit):
            key = f"monitor:{policy.class_id}:{day}:{current_hash}"
            try:
                with db.begin_nested():
                    reserved = db.execute(update(AutomaticReportBudget).where(
                        AutomaticReportBudget.day == day,
                        AutomaticReportBudget.used < settings().autonomous_daily_report_limit
                        ).values(used=AutomaticReportBudget.used + 1))
                    if reserved.rowcount:
                        run = enqueue(db, user, policy.class_id,
                            "Monitor this class against complete assessment records, learner support and timely follow-up. "
                            "Investigate changed evidence, delegate relevant specialist work, and prepare a report for the teacher and principal.",
                            trigger="monitor", dedupe_key=key)
                        queued = run.id
                        policy.last_hash = current_hash
            except IntegrityError:
                # A manual or automatic report already holds this class's slot.
                pass
        elif not policy.use_ai:
            policy.last_hash = current_hash
        policy.next_at = now + policy.interval_minutes * 60
        audit(db, user, policy.class_id, "autonomous_monitor_check",
              {"actor": "autonomous_monitor", "follow_up_changes": changed,
               "queued_run_id": queued, "source_hash": current_hash})
        db.commit()
    return True
