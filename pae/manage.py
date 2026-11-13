"""Explicit local setup. No accounts or fictional data are created at server startup."""
import argparse
import os
import secrets
from pathlib import Path
from sqlalchemy import select
from .db import Base, engine, SessionLocal, User, SchoolClass, Learner, Mark
from .security import hash_password


def bootstrap(sample=False):
    Base.metadata.create_all(engine)
    if not sample:
        print("Database schema initialised. Use bootstrap --sample for fictional evaluation records.")
        return
    credentials = []
    with SessionLocal() as db:
        users = []
        for email, role in [("principal@example.test", "principal"), ("teacher@example.test", "teacher"), ("other.teacher@example.test", "teacher")]:
            user = db.scalar(select(User).where(User.email == email))
            if not user:
                password = secrets.token_urlsafe(18)
                user = User(email=email, password_hash=hash_password(password), role=role, school="Fictional School")
                db.add(user)
                db.flush()
                credentials.append(f"{email}  {password}")
            users.append(user)
        for index, teacher in enumerate(users[1:], 1):
            c = db.scalar(select(SchoolClass).where(SchoolClass.teacher_id == teacher.id))
            if not c:
                c = SchoolClass(school="Fictional School", name=f"Grade 10 {'A' if index == 1 else 'B'}", subject="Mathematics", teacher_id=teacher.id)
                db.add(c)
                db.flush()
                for number, earned in enumerate([12, 15, 18, None], 1):
                    lid = f"G10{index}-{number:03d}"
                    db.add(Learner(id=lid, name=f"Fictional learner {index}-{number}", class_id=c.id))
                    db.flush()
                    db.add(Mark(class_id=c.id, learner_id=lid, assessment="Sample Test 1", topic="Algebra", earned=earned, possible=20))
        db.commit()
    if credentials:
        folder = Path(".local")
        folder.mkdir(exist_ok=True)
        file = folder / "bootstrap-login.txt"
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write("\n".join(credentials) + "\n")
        print("Fictional school created. Generated credentials are in .local/bootstrap-login.txt (gitignored).")
    else:
        print("Existing sample accounts preserved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "bootstrap"])
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--school")
    parser.add_argument("--email")
    args = parser.parse_args()
    bootstrap(sample=args.sample and args.command == "bootstrap")
    if args.school and args.email and not args.sample:
        from getpass import getpass
        password = getpass("New principal password (minimum 12 characters): ")
        if len(password) < 12:
            raise SystemExit("Password must be at least 12 characters")
        with SessionLocal() as db:
            if db.scalar(select(User.id).where(User.email == args.email.strip().lower())):
                raise SystemExit("Account already exists")
            db.add(User(email=args.email.strip().lower(), password_hash=hash_password(password), role="principal", school=args.school))
            db.commit()
        print("Principal account created. Sign in to register staff and classes.")
