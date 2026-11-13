"""Class-scoped report inboxes and a durable, opt-in TLS SMTP outbox."""
import copy
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage
from email.headerregistry import Address
from email.errors import HeaderParseError
from sqlalchemy import select, or_, update
from .db import (User, SchoolClass, ReportNotification, NotificationPreference,
                 EmailDelivery, utcnow)
from .config import settings


def recipients(db, class_id):
    school_class = db.get(SchoolClass, class_id)
    return db.scalars(select(User).where(User.school == school_class.school,
        or_(User.id == school_class.teacher_id, User.role == "principal"))).all()


def eligible(db, user, class_id):
    school_class = db.get(SchoolClass, class_id)
    return bool(user and school_class and user.school == school_class.school and (
        user.role == "principal" or (user.role == "teacher" and user.id == school_class.teacher_id)))


def usable_email(value):
    try:
        if "\r" in value or "\n" in value:
            return False
        address = Address(addr_spec=value)
        domain = address.domain.lower()
        return bool(address.username and "." in domain and not domain.endswith(
            (".test", ".invalid", ".localhost", ".example")) and domain not in {
                "example.com", "example.org", "example.net"})
    except (ValueError, IndexError, HeaderParseError):
        return False


def publish(db, class_id, kind, key, title, payload):
    created = []
    for recipient in recipients(db, class_id):
        existing = db.scalar(select(ReportNotification).where(
            ReportNotification.recipient_id == recipient.id, ReportNotification.dedupe_key == key))
        if existing:
            continue
        identifier = str(uuid.uuid4())
        item = ReportNotification(id=identifier, recipient_id=recipient.id, class_id=class_id,
            kind=kind, dedupe_key=key, title=title, payload=copy.deepcopy(payload))
        db.add(item)
        db.flush()
        preference = db.get(NotificationPreference, recipient.id)
        if preference and preference.email_enabled:
            db.add(EmailDelivery(id=identifier))
        created.append(identifier)
    return created


def public_notification(db, item):
    delivery = db.get(EmailDelivery, item.id)
    return {"id": item.id, "class_id": item.class_id, "kind": item.kind,
            "title": item.title, "payload": item.payload, "created_at": item.created_at,
            "read_at": item.read_at, "in_app_status": "delivered",
            "email_status": delivery.status if delivery else "not_requested",
            "email_last_error": delivery.last_error if delivery else ""}


def smtp_ready():
    config = settings()
    return bool(config.email_delivery_enabled and config.smtp_host and usable_email(config.smtp_from))


def send_smtp(item, recipient):
    config = settings()
    message = EmailMessage()
    message["From"], message["To"] = config.smtp_from, recipient.email
    message["Subject"] = " ".join(item.title.splitlines())
    domain = Address(addr_spec=config.smtp_from).domain
    message["Message-ID"] = f"<pae-{item.id}@{domain}>"
    payload = item.payload
    body = [item.title, f"Created: {item.created_at}", f"Class ID: {item.class_id}",
            "Scope: " + payload.get("scope", "authorised class report")]
    report = payload.get("report", payload)
    if report.get("summary"):
        body.append(report["summary"])
    for field in ["mean_percentage", "assessment_count", "marked_count", "missing_count"]:
        if field in report:
            body.append(f"{field}: {report[field]}")
    body.extend(report.get("recommendations", report.get("actions", [])))
    body.append("Open the school application for the source snapshot and delivery history.")
    message.set_content("\n\n".join(str(line) for line in body))
    context = ssl.create_default_context()
    if config.smtp_security == "ssl":
        connection = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=15, context=context)
    else:
        connection = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15)
    with connection:
        if config.smtp_security == "starttls":
            connection.ehlo()
            connection.starttls(context=context)
            connection.ehlo()
        if config.smtp_username:
            connection.login(config.smtp_username, config.smtp_password)
        connection.send_message(message)


def process_email(factory, sender=None):
    now, owner = time.time(), str(uuid.uuid4())
    with factory() as db:
        candidate = db.scalar(select(EmailDelivery.id).where(
            EmailDelivery.status.in_(["pending", "retrying", "sending", "blocked_configuration"]),
            EmailDelivery.next_at <= now, EmailDelivery.lease_until <= now).order_by(EmailDelivery.next_at).limit(1))
        if not candidate:
            return False
        changed = db.execute(update(EmailDelivery).where(EmailDelivery.id == candidate,
            EmailDelivery.lease_until <= now,
            EmailDelivery.status.in_(["pending", "retrying", "sending", "blocked_configuration"])
            ).values(status="sending", lease_owner=owner, lease_until=now + 120))
        if not changed.rowcount:
            db.rollback()
            return False
        db.commit()
        item = db.get(ReportNotification, candidate)
        recipient = db.get(User, item.recipient_id)
        preference = db.get(NotificationPreference, item.recipient_id)
        if not eligible(db, recipient, item.class_id) or not preference or not preference.email_enabled:
            status, error = "skipped", "Recipient permissions or email preference changed."
        elif not usable_email(recipient.email):
            status, error = "skipped", "Recipient address is not a usable delivery address."
        elif not smtp_ready():
            status, error = "blocked_configuration", "SMTP delivery is not enabled or configured."
        elif db.get(EmailDelivery, candidate).attempts >= settings().email_retry_attempts:
            status, error = "failed", "SMTP attempt budget exhausted."
        else:
            status, error = "", ""
            # Persist the attempt before contacting SMTP.
            db.execute(update(EmailDelivery).where(EmailDelivery.id == candidate,
                EmailDelivery.lease_owner == owner).values(attempts=EmailDelivery.attempts + 1))
            db.commit()
            try:
                (sender or send_smtp)(item, recipient)
                status = "sent"
            except Exception as failure:
                error = type(failure).__name__
                db.expire_all()
                attempts = db.get(EmailDelivery, candidate).attempts
                status = "retrying" if attempts < settings().email_retry_attempts else "failed"
        delay = 300 if status == "blocked_configuration" else 60
        db.execute(update(EmailDelivery).where(EmailDelivery.id == candidate,
            EmailDelivery.lease_owner == owner, EmailDelivery.lease_until > time.time()).values(
            status=status, last_error=error, lease_owner="", lease_until=0,
            next_at=now + delay if status in {"retrying", "blocked_configuration"} else 0,
            sent_at=utcnow() if status == "sent" else None))
        db.commit()
    return True

