"""A realistic, fully fictional demonstration school.

Run:  .venv/bin/python -m pae.demo_data
Then: cat .local/demo-login.txt   (generated passwords; gitignored)

Design
- Deterministic: the same records every time (fixed seed), so a demo is predictable.
- Idempotent: running it again changes nothing and never resets passwords.
- Realistic shape: 16 learners, 2 tests x 4 topics, a few missing marks,
  10 school days of attendance, and 3 support actions (one overdue).
- Fictional only: no real learners, schools or staff.
"""
import os
import random
import secrets
from datetime import date, timedelta
from pathlib import Path
from sqlalchemy import select
from .db import Base, engine, SessionLocal, User, SchoolClass, Learner, Mark, Attendance, Intervention
from .security import hash_password

SCHOOL = "Fictional Demo High School"
CLASS_NAME = "Grade 11 A"
SUBJECT = "Mathematics"
PRINCIPAL = "demo.principal@example.test"
TEACHER = "demo.teacher@example.test"
TOPICS = ["Algebra", "Functions", "Geometry", "Trigonometry"]
TESTS = ["Term 3 Test", "Term 4 Test"]
LEARNERS = ["Thando Mokoena", "Ayesha Patel", "Pieter van Wyk", "Lerato Dlamini", "Sipho Nkosi",
            "Emma Botha", "Kagiso Molefe", "Zanele Khumalo", "Johan Pretorius", "Naledi Mahlangu",
            "Musa Zulu", "Chloe Adams", "Bongani Sithole", "Refilwe Maseko", "Liam Naidoo", "Palesa Mofokeng"]
# Learner ability (0-1) and per-topic difficulty give a believable spread.
ABILITY = [0.88, 0.62, 0.79, 0.38, 0.91, 0.70, 0.66, 0.55, 0.93, 0.58, 0.74, 0.85, 0.45, 0.68, 0.81, 0.52]
DIFFICULTY = {"Algebra": 0.0, "Functions": -0.14, "Geometry": -0.04, "Trigonometry": -0.10}
MISSING = {("L11A-04", "Term 4 Test", "Geometry"), ("L11A-13", "Term 4 Test", "Trigonometry"),
           ("L11A-10", "Term 4 Test", "Functions")}


def build(db, today=None):
    """Create the demo school. Returns generated credentials, or [] if it already exists."""
    today = today or date.today()
    if db.scalar(select(User.id).where(User.email == PRINCIPAL)):
        return []
    rng = random.Random(20261114)
    credentials, users = [], {}
    for email, role in [(PRINCIPAL, "principal"), (TEACHER, "teacher")]:
        password = secrets.token_urlsafe(12)
        users[role] = User(email=email, password_hash=hash_password(password), role=role, school=SCHOOL)
        db.add(users[role])
        credentials.append((role, email, password))
    db.flush()
    school_class = SchoolClass(school=SCHOOL, name=CLASS_NAME, subject=SUBJECT, teacher_id=users["teacher"].id)
    db.add(school_class)
    db.flush()
    days = [today - timedelta(days=d) for d in range(14, 0, -1) if (today - timedelta(days=d)).weekday() < 5][-10:]
    for index, (name, ability) in enumerate(zip(LEARNERS, ABILITY), 1):
        learner_id = f"L11A-{index:02d}"
        db.add(Learner(id=learner_id, class_id=school_class.id, name=name))
        db.flush()
        for test in TESTS:
            drift = -0.06 if (test == TESTS[1] and index in (4, 13)) else 0.0  # two learners slipping
            for topic in TOPICS:
                if (learner_id, test, topic) in MISSING:
                    earned = None
                else:
                    score = ability + DIFFICULTY[topic] + drift + rng.uniform(-0.08, 0.08)
                    earned = float(max(0, min(20, round(20 * score))))
                db.add(Mark(class_id=school_class.id, learner_id=learner_id, assessment=test,
                            topic=topic, earned=earned, possible=20.0))
        absent_rate = 0.35 if index in (4, 13) else 0.06
        for day in days:
            status = "absent" if rng.random() < absent_rate else "present"
            if index == 10 and day == days[3]:
                status = "excused"
            db.add(Attendance(class_id=school_class.id, learner_id=learner_id, day=day.isoformat(), status=status))
    db.add_all([
        Intervention(class_id=school_class.id, learner_id="L11A-04", topic="Algebra",
                     activity="Factorising catch-up session", due=(today - timedelta(days=4)).isoformat(),
                     status="assigned"),
        Intervention(class_id=school_class.id, learner_id="L11A-02", topic="Functions",
                     activity="Worked examples and 5-question quiz", due=(today + timedelta(days=3)).isoformat(),
                     status="in_progress"),
        Intervention(class_id=school_class.id, learner_id="L11A-08", topic="Trigonometry",
                     activity="Special angles practice", due=(today - timedelta(days=6)).isoformat(),
                     status="completed", follow_up="Quiz improved from 9/20 to 15/20."),
    ])
    db.commit()
    return credentials


def write_credentials(credentials, folder=Path(".local")):
    folder.mkdir(exist_ok=True)
    file = folder / "demo-login.txt"
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"{SCHOOL} — fictional demonstration accounts\n")
        for role, email, password in credentials:
            stream.write(f"{role:<9} {email:<32} {password}\n")
    return file


def main():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        credentials = build(db)
    if credentials:
        file = write_credentials(credentials)
        print(f"Demo school created. Sign-in details: cat {file}")
    else:
        print("Demo school already exists. Sign-in details are in .local/demo-login.txt")


if __name__ == "__main__":
    main()
