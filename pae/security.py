import hashlib
import hmac
import secrets
import time
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .db import get_db, LoginSession, User

bearer = HTTPBearer(auto_error=False)


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


# A real hash of a random password. Checked when the email is unknown, so an
# unknown email takes as long as a wrong password (no account guessing by timing).
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def verify_password(password, stored):
    _, rounds, salt, digest = stored.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
    return hmac.compare_digest(actual, digest)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db=Depends(get_db)):
    if not credentials:
        raise HTTPException(401, "Sign in required")
    login = db.get(LoginSession, token_hash(credentials.credentials))
    if login and login.expires < time.time():
        db.delete(login)  # an expired session is never valid again; remove it
        db.commit()
        login = None
    if not login:
        raise HTTPException(401, "Session expired or invalid")
    user = db.get(User, login.user_id)
    if not user:
        raise HTTPException(401, "Account unavailable")
    return user
