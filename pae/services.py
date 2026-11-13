import csv
import io
import math
from datetime import date
from collections import Counter, defaultdict
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import SchoolClass, Learner, Mark, Attendance, Audit, Intervention


def allowed_class(db, user, class_id):
    c = db.get(SchoolClass, class_id)
    if not c or c.school != user.school or (user.role != "principal" and c.teacher_id != user.id):
        raise HTTPException(404, "Class not found")
    return c


def audit(db, user, class_id, action, detail):
    db.add(Audit(user_id=user.id, class_id=class_id, action=action, detail=detail))


def import_csv(db, user, class_id, content, kind):
    allowed_class(db, user, class_id)
    fields = {"learner_id", "assessment", "topic", "earned", "possible"} if kind == "marks" else {"learner_id", "day", "status"}
    try:
        reader = csv.DictReader(io.StringIO(content))
        if set(reader.fieldnames or []) != fields:
            raise ValueError("Use exactly these CSV columns: " + ",".join(sorted(fields)))
        rows = list(reader)
        if not 1 <= len(rows) <= 5000:
            raise ValueError("Upload between 1 and 5000 rows")
        learners = set(db.scalars(select(Learner.id).where(Learner.class_id == class_id)))
        # Load what already exists ONCE per upload instead of once per row.
        # Before: one SELECT per CSV row (up to 5,000). After: one SELECT per upload.
        if kind == "marks":
            existing_max, existing_keys = {}, set()
            for assessment, topic, learner_id, possible in db.execute(
                    select(Mark.assessment, Mark.topic, Mark.learner_id, Mark.possible).where(Mark.class_id == class_id)):
                existing_max.setdefault((assessment, topic), set()).add(possible)
                existing_keys.add((learner_id, assessment, topic))
        else:
            existing_days = {(lid, day) for lid, day in db.execute(select(Attendance.learner_id, Attendance.day).where(Attendance.learner_id.in_(learners)))} if learners else set()
        staged, seen, maxima = [], set(), {}
        for index, raw in enumerate(rows, 2):
            if any(v is None for v in raw.values()) or None in raw:
                raise ValueError(f"Row {index}: malformed CSV")
            r = {k: v.strip() for k, v in raw.items()}
            if r["learner_id"] not in learners:
                raise ValueError(f"Row {index}: learner does not belong to this class")
            if kind == "marks":
                key = (r["learner_id"], r["assessment"], r["topic"])
                if not r["assessment"] or not r["topic"] or max(len(r["assessment"]), len(r["topic"])) > 120:
                    raise ValueError(f"Row {index}: assessment and topic are required (max 120 characters)")
                possible = float(r["possible"])
                earned = float(r["earned"]) if r["earned"] else None
                if not math.isfinite(possible) or possible <= 0 or (earned is not None and (not math.isfinite(earned) or not 0 <= earned <= possible)):
                    raise ValueError(f"Row {index}: marks outside valid range")
                topic_key = (r["assessment"], r["topic"])
                maxima.setdefault(topic_key, possible)
                if maxima[topic_key] != possible or any(m != possible for m in existing_max.get(topic_key, ())):
                    raise ValueError(f"Row {index}: inconsistent maximum for assessment/topic")
                if key in existing_keys:
                    raise ValueError(f"Row {index}: mark already exists; use an audited correction")
                obj = Mark(class_id=class_id, learner_id=r["learner_id"], assessment=r["assessment"], topic=r["topic"], possible=possible, earned=earned)
            else:
                day = date.fromisoformat(r["day"]).isoformat()
                key = (r["learner_id"], day)
                if r["status"] not in {"present", "absent", "excused", "unknown"}:
                    raise ValueError(f"Row {index}: invalid attendance status")
                if key in existing_days:
                    raise ValueError(f"Row {index}: attendance already recorded")
                obj = Attendance(class_id=class_id, learner_id=r["learner_id"], day=day, status=r["status"])
            if key in seen:
                raise ValueError(f"Row {index}: duplicate record in upload")
            seen.add(key)
            staged.append(obj)
    except (ValueError, csv.Error) as e:
        raise HTTPException(422, str(e)) from e
    db.add_all(staged)
    audit(db, user, class_id, f"import_{kind}", {"rows": len(staged)})
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "Records changed during import. Refresh and check for duplicates.") from error
    return {"inserted": len(staged)}


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
    # Group once: O(marks) instead of O(learners x marks).
    marked_by_learner = defaultdict(list)
    for m in marked:
        marked_by_learner[m.learner_id].append(m)
    learner_results = []
    for learner in learners:
        rows = marked_by_learner[learner.id]
        total = sum(m.possible for m in rows)
        mean = round(100 * sum(m.earned for m in rows) / total, 2) if total else None
        learner_results.append({"learner_id": learner.id, "mean_percentage": mean,
                                "marked_count": len(rows), "expected_marks": len(pairs),
                                "support_review": mean is not None and mean < c.support_threshold})
    return {"class_id": c.id, "class_name": c.name, "subject": c.subject,
            "learner_count": len(learners), "expected_marks": expected,
            "marked_count": len(marked), "missing_count": expected - len(marked),
            "mean_percentage": round(100 * sum(m.earned for m in marked) / possible, 2) if possible else None,
            "topics": topic_results, "learners": learner_results, "support_threshold": c.support_threshold,
            "attendance_recorded_status_counts": dict(Counter(a.status for a in attendance)),
            "interventions": [{"id": i.id, "learner_id": i.learner_id, "topic": i.topic, "activity": i.activity, "due": i.due, "status": i.status, "follow_up": i.follow_up} for i in interventions],
            "definitions": {"mean_percentage": "Sum earned / sum possible for marked topic records only; this is not an official term or promotion mark.", "missing_count": "Expected learner-assessment-topic records minus marked records. A blank is never silently zero.", "attendance": "Counts of supplied register records only. No inference about missing dates or causes of absence."}}
