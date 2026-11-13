"""Scoped CSV feeds, durable batches and unattended watched-source ingestion."""
import hashlib
import os
import secrets
import stat
import time
import uuid
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import AssessmentSource, IngestionBatch, User, FollowUp, SchoolClass, utcnow
from .security import token_hash
from .services import allowed_class, import_csv, audit

MAX_BYTES = 2_000_000


def source_folder(identifier):
    # IDs come from our UUID generator; no user paths are accepted.
    return Path(settings().ingestion_inbox_root).resolve() / str(uuid.UUID(identifier))


def create_source(db, user, class_id, name, kind, allow_corrections=False):
    allowed_class(db, user, class_id)
    key = secrets.token_urlsafe(48)
    source = AssessmentSource(id=str(uuid.uuid4()), class_id=class_id, owner_id=user.id,
        name=name.strip(), kind=kind, allow_corrections=allow_corrections,
        ingestion_token_hash=token_hash(key))
    db.add(source)
    db.flush()
    audit(db, user, class_id, "assessment_source_registered", {"source_id": source.id, "kind": kind})
    return source, key


def public_source(source):
    return {"id": source.id, "class_id": source.class_id, "name": source.name,
        "kind": source.kind, "enabled": source.enabled, "allow_corrections": source.allow_corrections,
        "inbox_folder": str(source_folder(source.id)), "upload_endpoint": "/ingestion/" + source.id}


def accept(db, source, content, filename="scheduled-export.csv"):
    if not source.enabled:
        raise HTTPException(409, "This source is disabled")
    owner = db.get(User, source.owner_id)
    if not owner:
        raise HTTPException(403, "Source owner no longer has class access")
    allowed_class(db, owner, source.class_id)
    if not content or len(content.encode("utf-8")) > MAX_BYTES:
        raise HTTPException(422, "CSV must be nonempty and at most 2 MB")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = db.scalar(select(IngestionBatch).where(IngestionBatch.source_id == source.id,
                                                     IngestionBatch.content_hash == digest))
    if existing:
        return existing
    batch = IngestionBatch(id=str(uuid.uuid4()), source_id=source.id,
        filename=Path(filename).name[:200], content=content, content_hash=digest)
    db.add(batch)
    db.flush()
    return batch


def authenticate_source(db, identifier, key):
    source = db.get(AssessmentSource, identifier)
    if (not source or not key or not source.ingestion_token_hash or
            not secrets.compare_digest(source.ingestion_token_hash, token_hash(key))):
        raise HTTPException(401, "Invalid ingestion source credentials")
    return source


def scan_sources(factory):
    """The producer should atomically rename a completed .csv into this directory."""
    count = 0
    with factory() as db:
        sources = list(db.scalars(select(AssessmentSource).where(AssessmentSource.enabled.is_(True))))
    for source in sources:
        folder = source_folder(source.id)
        if folder.is_symlink():
            continue
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive = folder / "processed"
        if archive.is_symlink():
            continue
        archive.mkdir(exist_ok=True, mode=0o700)
        for path in sorted(folder.glob("*.csv"))[:20]:
            descriptor = None
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                before = os.fstat(descriptor)
                if (not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES or
                        time.time() - before.st_mtime < settings().ingestion_settle_seconds):
                    continue
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = None
                    raw = stream.read(MAX_BYTES + 1)
                    after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    continue
                content = raw.decode("utf-8-sig")
                with factory() as db:
                    current = db.get(AssessmentSource, source.id)
                    batch = accept(db, current, content, path.name)
                    identifier = batch.id
                    db.commit()
                # Do not move a file replaced while it was being persisted.
                current_stat = path.lstat()
                if (current_stat.st_ino, current_stat.st_mtime_ns, current_stat.st_size) == (
                        before.st_ino, before.st_mtime_ns, before.st_size):
                    path.replace(archive / (identifier + ".csv"))
                count += 1
            except (OSError, UnicodeError, HTTPException, IntegrityError):
                # Invalid encoding/files stay in the source directory for correction.
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    return count


def process_batch(factory):
    now = time.time()
    with factory() as db:
        batch = db.scalar(select(IngestionBatch).join(AssessmentSource).where(
            AssessmentSource.enabled.is_(True),
            IngestionBatch.status.in_(["pending", "waiting_evidence"]), IngestionBatch.next_at <= now
            ).order_by(IngestionBatch.created_at, IngestionBatch.id)
            .with_for_update(skip_locked=True).limit(1))
        if not batch:
            return False
        identifier, source_id = batch.id, batch.source_id
        source = db.get(AssessmentSource, source_id)
        if not source.enabled:
            return False
        owner = db.get(User, source.owner_id)
        try:
            result = import_csv(db, owner, source.class_id, batch.content, source.kind,
                upsert_missing=True, allow_corrections=source.allow_corrections,
                source_batch=identifier, commit=False)
            batch.status, batch.result = "completed", result
            task = db.scalar(select(FollowUp).where(FollowUp.class_id == source.class_id,
                FollowUp.issue_key == "ingestion:" + identifier))
            if task:
                task.status, task.outcome, task.updated_at = "resolved", "Source records validated and captured automatically.", utcnow()
            db.commit()
        except (HTTPException, IntegrityError) as error:
            db.rollback()
            batch, source = db.get(IngestionBatch, identifier), db.get(AssessmentSource, source_id)
            message = error.detail if isinstance(error, HTTPException) else "Conflicting records; source reconciliation is required."
            waiting = "Learner" in str(message) or "learner" in str(message)
            batch.status = "waiting_evidence" if waiting else "requires_review"
            batch.next_at = now + 60
            batch.result = {"message": message, "action": "Check the original records. No marks were guessed or partially saved."}
            school_class = db.get(SchoolClass, source.class_id)
            task = db.scalar(select(FollowUp).where(FollowUp.class_id == source.class_id,
                FollowUp.issue_key == "ingestion:" + identifier))
            if not task:
                task = FollowUp(class_id=source.class_id, issue_key="ingestion:" + identifier,
                    assigned_to=school_class.teacher_id, status="open",
                    detail={"kind": "source_validation", "batch_id": identifier, "message": message},
                    due_at=now + 86400)
                db.add(task)
            from .notifications import publish
            publish(db, source.class_id, "source_attention", "source:" + identifier,
                "Assessment source needs evidence", {"batch_id": identifier,
                "report": {"summary": message}, "scope": "No partial capture was committed."})
            db.commit()
    return True
