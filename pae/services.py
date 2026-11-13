import csv
import io
import math
from datetime import date
from collections import Counter, defaultdict
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from .db import SchoolClass, Learner, Mark, Attendance, Audit, Intervention, MonitoringPolicy


def allowed_class(db, user, class_id):
    c = db.get(SchoolClass, class_id)
    if not user or not c or c.school != user.school or (user.role != "principal" and c.teacher_id != user.id):
        raise HTTPException(404, "Class not found")
    return c


def audit(db, user, class_id, action, detail):
    db.add(Audit(user_id=user.id, class_id=class_id, action=action, detail=detail))


def wake_monitor(db, class_id):
    db.execute(update(MonitoringPolicy).where(MonitoringPolicy.class_id == class_id,
        MonitoringPolicy.enabled.is_(True), MonitoringPolicy.on_record_change.is_(True)).values(next_at=0))


def import_csv(db, user, class_id, content, kind, *, upsert_missing=False,
               allow_corrections=False, source_batch=None, commit=True):
    allowed_class(db, user, class_id)
    fields = {"learner_id", "assessment", "topic", "earned", "possible"} if kind == "marks" else {"learner_id", "day", "status"}
    staged, changes, unchanged = [], [], 0
    try:
        reader = csv.DictReader(io.StringIO(content))
        if set(reader.fieldnames or []) != fields or len(reader.fieldnames or []) != len(fields):
            raise ValueError("Use exactly these CSV columns: " + ",".join(sorted(fields)))
        rows = list(reader)
        if not 1 <= len(rows) <= 5000:
            raise ValueError("Upload between 1 and 5000 rows")
        learners = set(db.scalars(select(Learner.id).where(Learner.class_id == class_id)))
        seen, maxima = set(), {}
        for index, raw in enumerate(rows, 2):
            if any(v is None for v in raw.values()) or None in raw:
                raise ValueError(f"Row {index}: malformed CSV")
            row = {key: value.strip() for key, value in raw.items()}
            if row["learner_id"] not in learners:
                raise ValueError(f"Row {index}: learner does not belong to this class")
            if kind == "marks":
                key = (row["learner_id"], row["assessment"], row["topic"])
                if not row["assessment"] or not row["topic"] or max(len(row["assessment"]), len(row["topic"])) > 120:
                    raise ValueError(f"Row {index}: assessment and topic are required (max 120 characters)")
                possible = float(row["possible"])
                earned = float(row["earned"]) if row["earned"] else None
                if not math.isfinite(possible) or possible <= 0 or (earned is not None and
                        (not math.isfinite(earned) or not 0 <= earned <= possible)):
                    raise ValueError(f"Row {index}: marks outside valid range")
                topic_key = (row["assessment"], row["topic"])
                existing = db.scalars(select(Mark).where(Mark.class_id == class_id,
                    Mark.assessment == row["assessment"], Mark.topic == row["topic"])).all()
                maxima.setdefault(topic_key, possible)
                if maxima[topic_key] != possible or any(mark.possible != possible for mark in existing):
                    raise ValueError(f"Row {index}: inconsistent maximum for assessment/topic")
                old = next((mark for mark in existing if mark.learner_id == row["learner_id"]), None)
                value, attribute = earned, "earned"
                obj = Mark(class_id=class_id, learner_id=row["learner_id"], assessment=row["assessment"],
                           topic=row["topic"], possible=possible, earned=earned)
            else:
                day = date.fromisoformat(row["day"]).isoformat()
                key = (row["learner_id"], day)
                if row["status"] not in {"present", "absent", "excused", "unknown"}:
                    raise ValueError(f"Row {index}: invalid attendance status")
                old = db.scalar(select(Attendance).where(Attendance.learner_id == row["learner_id"], Attendance.day == day))
                value, attribute = row["status"], "status"
                obj = Attendance(class_id=class_id, learner_id=row["learner_id"], day=day, status=value)
            if key in seen:
                raise ValueError(f"Row {index}: duplicate record in upload")
            seen.add(key)
            if old:
                if not upsert_missing:
                    raise ValueError(f"Row {index}: record already exists; use an audited correction")
                previous = getattr(old, attribute)
                if previous == value or (kind == "marks" and value is None):
                    unchanged += 1
                    continue
                missing = previous is None if kind == "marks" else previous == "unknown"
                if not missing and not allow_corrections:
                    raise ValueError(f"Row {index}: source conflicts with a captured value; source correction authority is disabled")
                changes.append((old, attribute, previous, value))
            else:
                staged.append(obj)
    except (ValueError, csv.Error) as error:
        raise HTTPException(422, str(error)) from None
    try:
        db.add_all(staged)
        for obj, attribute, before, after in changes:
            setattr(obj, attribute, after)
            audit(db, user, class_id, "source_record_corrected", {
                "record_type": kind, "record_id": obj.id, "before": before,
                "after": after, "source_batch": source_batch})
        changed_records = len(staged) + len(changes)
        capture_id = None
        if changed_records:
            audit(db, user, class_id, f"import_{kind}", {
                "inserted": len(staged), "updated": len(changes), "source_batch": source_batch})
            wake_monitor(db, class_id)
            from .captures import record_capture
            capture_id = record_capture(db, user, class_id,
                "source_" + kind if source_batch else "import_" + kind, changed_records)
        if commit:
            db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "Records changed during import. Refresh and check for duplicates.") from None
    return {"inserted": len(staged), "updated": len(changes), "unchanged": unchanged, "capture_id": capture_id}


def evidence(db, user, class_id):
    c = allowed_class(db, user, class_id)
    learners = db.scalars(select(Learner).where(Learner.class_id == class_id)).all()
    marks = db.scalars(select(Mark).where(Mark.class_id == class_id)).all()
    attendance = db.scalars(select(Attendance).where(Attendance.class_id == class_id)).all()
    marked = [m for m in marks if m.earned is not None]
    pairs = {(m.assessment, m.topic) for m in marks}
    possible = sum(m.possible for m in marked)
    topics = defaultdict(list)
    for m in marks:
        topics[m.topic].append(m)
    topic_results = []
    for topic, rows in sorted(topics.items()):
        scored = [m for m in rows if m.earned is not None]
        max_total = sum(m.possible for m in scored)
        topic_results.append({"topic": topic, "marked": len(scored), "mean_percentage": round(100 * sum(m.earned for m in scored) / max_total, 2) if max_total else None})
    expected = len(learners) * len(pairs)
    interventions = db.scalars(select(Intervention).where(Intervention.class_id == class_id)).all()
    learner_results = []
    for learner in learners:
        rows = [m for m in marked if m.learner_id == learner.id]
        total = sum(m.possible for m in rows)
        mean = round(100 * sum(m.earned for m in rows) / total, 2) if total else None
        learner_results.append({"learner_id": learner.id, "mean_percentage": mean,
                                "marked_count": len(rows), "expected_marks": len(pairs),
                                "support_review": mean is not None and mean < c.support_threshold})
    return {"class_id": c.id, "class_name": c.name, "subject": c.subject,
            "assessment_count": len({m.assessment for m in marks}),
            "assessment_topic_count": len(pairs),
            "learner_count": len(learners), "expected_marks": expected,
            "marked_count": len(marked), "missing_count": expected - len(marked),
            "mean_percentage": round(100 * sum(m.earned for m in marked) / possible, 2) if possible else None,
            "topics": topic_results, "learners": learner_results, "support_threshold": c.support_threshold,
            "attendance_recorded_status_counts": dict(Counter(a.status for a in attendance)),
            "interventions": [{"id": i.id, "learner_id": i.learner_id, "topic": i.topic, "activity": i.activity, "due": i.due, "status": i.status, "follow_up": i.follow_up} for i in interventions],
            "definitions": {"mean_percentage": "Sum earned / sum possible for marked topic records only; this is not an official term or promotion mark.", "missing_count": "Expected learner-assessment-topic records minus marked records. A blank is never silently zero.", "attendance": "Counts of supplied register records only. No inference about missing dates or causes of absence."}}
