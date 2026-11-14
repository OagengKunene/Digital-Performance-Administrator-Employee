"""Durable photo -> demo marks -> student report -> numerical review -> inbox."""
import copy
from decimal import Decimal
import hashlib
import io
from pathlib import Path
import re
import subprocess
import time
import uuid

from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import ScriptPhoto, User, Learner, SchoolClass, utcnow
from .notifications import publish
from .script_reader import ScriptReading, choose_reader, normalise_image, read_script
from .services import allowed_class, audit

ACTIVE = {"queued", "reading", "validating", "reporting", "reviewing"}


def event(stage, message):
    return {"stage": stage, "message": message, "at": utcnow()}


def accept_photo(db, user, class_id, raw, filename, reader="auto"):
    allowed_class(db, user, class_id)
    try:
        reader = choose_reader(reader)
        image = normalise_image(raw)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    digest = hashlib.sha256(image).hexdigest()
    existing = db.scalar(select(ScriptPhoto).where(ScriptPhoto.class_id == class_id,
        ScriptPhoto.image_hash == digest, ScriptPhoto.reader == reader))
    if existing:
        return existing
    pending = db.scalar(select(func.count()).select_from(ScriptPhoto).where(
        ScriptPhoto.class_id == class_id, ScriptPhoto.status.in_(ACTIVE)))
    if pending >= 20:
        raise HTTPException(409, "This class already has 20 photos processing. Let them finish before uploading more.")
    item = ScriptPhoto(id=str(uuid.uuid4()), class_id=class_id, user_id=user.id,
        filename=Path(filename).name[:160] or "marked-script.jpg", image=image,
        image_hash=digest, reader=reader, events=[event("queued", "Photo received; automatic processing queued.")])
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
            audit(db, user, class_id, "script_photo_uploaded", {"photo_id": item.id, "reader": reader, "mode": "demo"})
    except IntegrityError:
        item = db.scalar(select(ScriptPhoto).where(ScriptPhoto.class_id == class_id,
            ScriptPhoto.image_hash == digest, ScriptPhoto.reader == reader))
        if not item:
            raise
    return item


def public_photo(item, *, detail=True):
    result = {"id": item.id, "filename": item.filename, "status": item.status,
        "reader": item.reader, "created_at": item.created_at, "attempts": item.attempts,
        "issues": item.issues, "events": item.events, "mode": "demo",
        "scope": "Demo capture from this photo; official class marks are unchanged."}
    if detail:
        result.update(extraction=item.extraction, report=item.report,
            original_extraction=item.original_extraction)
    return result


def get_photo(db, user, class_id, identifier):
    allowed_class(db, user, class_id)
    item = db.get(ScriptPhoto, identifier)
    if not item or item.class_id != class_id:
        raise HTTPException(404, "Script photo not found")
    return item


def check_reading(reading):
    """Check sums independently of any totals asserted by OCR/the model."""
    issues = list(reading.issues)
    seen = set()
    for row in reading.questions:
        key = re.sub(r"^(?:question|q)\s*", "", row.question.lower()).replace(" ", "")
        key = ".".join(str(int(part)) if part.isdigit() else part for part in key.split("."))
        if key in seen:
            issues.append(f"{row.question}: duplicate question; check for double counting.")
        seen.add(key)
        if row.uncertain or row.earned is None or row.possible is None:
            issues.append(f"{row.question}: an awarded score or maximum is unclear.")
        elif row.earned > row.possible:
            issues.append(f"{row.question}: awarded score exceeds the maximum.")
    if any(other.startswith(key + ".") for key in seen for other in seen):
        issues.append("Parent and subquestion scores overlap; remove double counting.")
    if not reading.questions and (reading.total_earned is None or reading.total_possible is None):
        issues.append("No complete awarded score / maximum was readable. Upload a clearer marked page.")
    if (reading.total_earned is None) != (reading.total_possible is None):
        issues.append("The written total is incomplete.")
    if reading.total_earned is not None and reading.total_possible is not None and reading.total_earned > reading.total_possible:
        issues.append("Written total exceeds the maximum.")
    if reading.questions and all(q.earned is not None and q.possible is not None for q in reading.questions):
        earned = sum(Decimal(str(q.earned)) for q in reading.questions)
        possible = sum(Decimal(str(q.possible)) for q in reading.questions)
        if reading.total_earned is not None and (earned != Decimal(str(reading.total_earned)) or
                possible != Decimal(str(reading.total_possible))):
            issues.append("Question sums disagree with the written total. Check omitted pages, subtotals or unclear digits.")
    return list(dict.fromkeys(issues))


def student_report(reading, school_class, learner=None):
    if check_reading(reading):
        raise ValueError("Unresolved transcription issues")
    rows = [q.model_dump() for q in reading.questions]
    if not rows:
        rows = [{"question": "Overall", "topic": "Unspecified topic", "earned": reading.total_earned,
                 "possible": reading.total_possible, "evidence": "Written total only", "uncertain": False}]
    earned = float(sum(Decimal(str(row["earned"])) for row in rows))
    possible = float(sum(Decimal(str(row["possible"])) for row in rows))
    percentage = round(earned / possible * 100, 2)
    grouped = {}
    for row in rows:
        row["percentage"] = round(100 * row["earned"] / row["possible"], 2)
        group = grouped.setdefault(row["topic"], {"topic": row["topic"], "earned": 0.0, "possible": 0.0})
        group["earned"] += row["earned"]
        group["possible"] += row["possible"]
    topics = [{**group, "percentage": round(100 * group["earned"] / group["possible"], 2)} for group in grouped.values()]
    strengths = [f"{t['topic']}: {t['earned']:g}/{t['possible']:g} ({t['percentage']:g}%)."
                 for t in topics if t["percentage"] >= 70 and t["topic"] != "Unspecified topic"]
    support = [f"{t['topic']}: {t['earned']:g}/{t['possible']:g} ({t['percentage']:g}%)."
               for t in topics if t["percentage"] < school_class.support_threshold and t["topic"] != "Unspecified topic"]
    name = learner.name if learner else (reading.learner_name or reading.learner_id or "Unidentified learner")
    recommendations = [f"Review the marked {t['topic']} questions with the learner, provide targeted practice, "
                       "and check the next marked task for improvement."
                       for t in topics if t["percentage"] < school_class.support_threshold and t["topic"] != "Unspecified topic"]
    if not recommendations:
        recommendations.append("Review the lost marks on this test and use the next marked task to check progress.")
    limitations = ["This is one photographed marked script, not an official term result or a diagnosis.",
        "Review checks transcription structure, arithmetic and report metrics; it does not certify every digit against the original paper.",
        "Demo marks are stored with this photo; official class marks are unchanged."]
    if not learner:
        limitations.append("The learner identity was not matched to this class roster; no learner history was joined.")
    if not reading.questions:
        limitations.append("Only an overall total was readable; question-level strengths and weaknesses cannot be established.")
    if any(t["topic"] == "Unspecified topic" for t in topics):
        limitations.append("Some topic labels were not visible; no topic was inferred from an answer.")
    return {"kind": "student_script_demo", "learner_id": reading.learner_id, "learner_name": name,
        "roster_matched": bool(learner), "assessment": reading.assessment,
        "subject": reading.subject or school_class.subject, "class_name": school_class.name,
        "earned": earned, "possible": possible, "mean_percentage": percentage,
        "marked_count": len(reading.questions), "support_threshold": school_class.support_threshold,
        "support_review": percentage < school_class.support_threshold,
        "summary": f"{name}: {earned:g}/{possible:g} ({percentage:g}%) on {reading.assessment}. "
            + ("The script result is below the class support-review threshold." if percentage < school_class.support_threshold
               else "The script result is at or above the class support-review threshold."),
        "questions": rows, "topics": topics, "strengths": strengths, "support_areas": support,
        "recommendations": recommendations, "limitations": limitations}


def review_report(report, reading):
    """A second pass recomputes source metrics, independent of the report builder."""
    if reading.questions:
        numerator = sum(Decimal(str(q.earned)) for q in reading.questions)
        denominator = sum(Decimal(str(q.possible)) for q in reading.questions)
    else:
        numerator, denominator = Decimal(str(reading.total_earned)), Decimal(str(reading.total_possible))
    expected = round(float(100 * numerator / denominator), 2)
    issues = check_reading(reading)
    if (report.get("earned"), report.get("possible"), report.get("mean_percentage")) != (float(numerator), float(denominator), expected):
        issues.append("Report totals or percentage differ from the source transcription.")
    if reading.questions:
        expected_rows = [(q.question, q.topic, q.earned, q.possible) for q in reading.questions]
        actual_rows = [(q.get("question"), q.get("topic"), q.get("earned"), q.get("possible")) for q in report.get("questions", [])]
        if actual_rows != expected_rows:
            issues.append("Report question rows differ from the source transcription.")
    return {"status": "passed" if not issues else "blocked", "issues": issues,
            "checked_earned": float(numerator), "checked_possible": float(denominator),
            "checked_percentage": expected, "scope": "Arithmetic and report consistency against the saved transcription."}


def correct_reading(db, user, class_id, item, reading, reason):
    allowed_class(db, user, class_id)
    if item.status not in {"requires_review", "failed"}:
        raise HTTPException(409, "Only a blocked reading can be corrected.")
    before = copy.deepcopy(item.extraction)
    item.extraction = reading.model_dump()
    item.report, item.issues, item.status = {}, [], "validating"
    item.lease_owner, item.lease_until, item.next_at = "", 0, 0
    item.events = [*item.events, event("correction", "Staff corrected the transcription; automatic validation resumes.")]
    audit(db, user, class_id, "script_photo_transcription_corrected",
        {"photo_id": item.id, "before": before, "after": item.extraction, "reason": reason})


def process_photo(factory, reader_fn=None):
    now, owner = time.time(), str(uuid.uuid4())
    with factory() as db:
        identifier = db.scalar(select(ScriptPhoto.id).where(ScriptPhoto.status.in_(ACTIVE),
            ScriptPhoto.lease_until <= now, ScriptPhoto.next_at <= now)
            .order_by(ScriptPhoto.created_at, ScriptPhoto.id).limit(1))
        if not identifier:
            return False
        changed = db.execute(update(ScriptPhoto).where(ScriptPhoto.id == identifier,
            ScriptPhoto.status.in_(ACTIVE), ScriptPhoto.lease_until <= now, ScriptPhoto.next_at <= now).values(
                lease_owner=owner, lease_until=now + settings().photo_timeout_seconds + 30))
        if not changed.rowcount:
            db.rollback()
            return False
        item = db.get(ScriptPhoto, identifier)
        try:
            allowed_class(db, db.get(User, item.user_id), item.class_id)
        except HTTPException:
            item.status, item.lease_owner, item.lease_until = "authorisation_blocked", "", 0
            db.commit()
            return True
        phase = item.status
        if phase in {"queued", "reading"}:
            phase = "reading"
            if item.attempts >= 3:
                item.status, item.issues = "failed", ["Photo reader attempt budget exhausted. Upload a clearer image or review the transcription."]
                item.lease_owner, item.lease_until = "", 0
                db.commit()
                return True
            item.status, item.attempts = "reading", item.attempts + 1
            item.events = [*item.events, event("reading", "Reading the marked photo using " + item.reader + ".")]
        image, reader = item.image, item.reader
        extraction, report = copy.deepcopy(item.extraction), copy.deepcopy(item.report)
        db.commit()
    try:
        changes, message = {}, ""
        if phase == "reading":
            result = ScriptReading.model_validate((reader_fn or read_script)(image, reader)).model_dump()
            changes = {"extraction": result, "original_extraction": result, "status": "validating", "issues": []}
            message = "Awarded marks extracted; checking completeness and totals."
        elif phase == "validating":
            issues = check_reading(ScriptReading.model_validate(extraction))
            changes = {"status": "requires_review" if issues else "reporting", "issues": issues}
            message = "Transcription needs attention; no final report published." if issues else "Score ranges and written totals checked; building the student report."
        elif phase == "reporting":
            with factory() as db:
                item = db.get(ScriptPhoto, identifier)
                school_class = db.get(SchoolClass, item.class_id)
                learner = db.scalar(select(Learner).where(Learner.id == extraction.get("learner_id", ""),
                    Learner.class_id == item.class_id))
                report = student_report(ScriptReading.model_validate(extraction), school_class, learner)
            changes = {"report": report, "status": "reviewing"}
            message = "Student report drafted; independently recomputing its metrics."
        else:
            review = review_report(report, ScriptReading.model_validate(extraction))
            report["review"] = review
            changes = {"report": report, "status": "completed" if review["status"] == "passed" else "requires_review", "issues": review["issues"]}
            message = "Report review passed; delivering to the teacher and principal inboxes." if review["status"] == "passed" else "Review blocked report publication."
    except Exception as error:
        # Never store a provider exception's raw message, credentials or image.
        from .ai_diagnostics import describe_ai_error
        detail = describe_ai_error(error)
        code = getattr(getattr(error, "response", None), "status_code", None)
        transient = isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) or code in {429, 500, 502, 503, 504} or detail["error_code"] in {"provider_connection", "ai_timeout"}
        with factory() as db:
            attempts = db.get(ScriptPhoto, identifier).attempts
        retry = phase == "reading" and transient and attempts < 3
        changes = {"status": "reading" if retry else "requires_review",
            "issues": ["The photo could not be read reliably (" + type(error).__name__ + "). Try a clearer photo or another configured reader."],
            "next_at": time.time() + 15 if retry else 0}
        message = "Photo reader temporarily unavailable; retry scheduled." if retry else "Photo reading needs attention; no marks were guessed."
    with factory() as db:
        # Fence every update and publication against a superseded/crashed worker.
        changed = db.execute(update(ScriptPhoto).where(ScriptPhoto.id == identifier,
            ScriptPhoto.lease_owner == owner, ScriptPhoto.lease_until > time.time())
            .values(**changes, lease_owner="", lease_until=0))
        if not changed.rowcount:
            db.rollback()
            return True
        item = db.get(ScriptPhoto, identifier)
        try:
            allowed_class(db, db.get(User, item.user_id), item.class_id)
        except HTTPException:
            item.status = "authorisation_blocked"
            db.commit()
            return True
        item.events = [*item.events, event(item.status, message)]
        if item.status == "completed":
            publish(db, item.class_id, "script_evaluation", "script:" + item.id,
                "Student marked-script demo report", {"photo_id": item.id, "scope": public_photo(item)["scope"],
                    "report": copy.deepcopy(item.report)})
            audit(db, db.get(User, item.user_id), item.class_id, "script_photo_report_delivered", {"photo_id": item.id})
        db.commit()
    return True


def sample_script(learner_id="DEMO-001"):
    """An exact printed fixture for actual OCR; no embedded/precomputed results."""
    image = Image.new("RGB", (1400, 1200), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=34)
    lines = ["MARKED TEST SCRIPT - FICTIONAL DEMO", "Learner ID: " + learner_id,
        "Name: Demo learner", "Assessment: Mathematics diagnostic", "Subject: Mathematics", "",
        "Q1 | Algebra | 8 / 10", "Q2 | Equations | 5 / 10", "Q3 | Geometry | 3 / 10",
        "Q4 | Data handling | 9 / 10", "", "TOTAL: 25 / 40", "", "Already marked - read the awarded scores."]
    for index, line in enumerate(lines):
        draw.text((65, 65 + index * 72), line, font=font, fill="#17243b")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()
