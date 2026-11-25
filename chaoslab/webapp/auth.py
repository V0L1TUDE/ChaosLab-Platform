"""
ChaosLab Online MVP - authentication
====================================
scrypt password hashing, opaque random session tokens (only their SHA-256 is stored),
HttpOnly cookie sessions, and a small in-memory login throttle.
"""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Any, Dict, Optional

from . import config, db

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class AuthError(Exception):
    """Raised for validation/authentication failures. `status` maps to the HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


# Used to spend the same CPU time when an email is unknown (avoids user enumeration by timing).
_DUMMY_HASH = hash_password("chaoslab-dummy-password")

# --- login throttle: max 8 failures / 5 minutes per email -------------------------------------
_attempts: Dict[str, list] = {}
_attempts_lock = threading.Lock()
_WINDOW, _MAX_FAILURES = 300, 8


def _throttled(key: str) -> bool:
    now = time.time()
    with _attempts_lock:
        recent = [t for t in _attempts.get(key, []) if now - t < _WINDOW]
        _attempts[key] = recent
        return len(recent) >= _MAX_FAILURES


def _record_failure(key: str) -> None:
    with _attempts_lock:
        _attempts.setdefault(key, []).append(time.time())


def _clear_failures(key: str) -> None:
    with _attempts_lock:
        _attempts.pop(key, None)


# --- user + session management ----------------------------------------------------------------
def public_user(row) -> Dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "email": row["email"], "created_at": row["created_at"]}


def register_user(name: Any, email: Any, password: Any) -> Dict[str, Any]:
    if not isinstance(name, str) or not isinstance(email, str) or not isinstance(password, str):
        raise AuthError("name, email and password are required strings.")
    name, email = name.strip(), email.strip().lower()
    if not 1 <= len(name) <= 80:
        raise AuthError("Name must be between 1 and 80 characters.")
    if not _EMAIL_RE.match(email):
        raise AuthError("Please provide a valid email address.")
    if len(password) < config.MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {config.MIN_PASSWORD_LENGTH} characters.")
    if len(password) > 256:
        raise AuthError("Password is too long (max 256 characters).")

    with db.session() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise AuthError("An account with this email already exists.", 409)
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (name, email, hash_password(password), db.utcnow()),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return public_user(row)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=config.SESSION_TTL_SECONDS)
    with db.session() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(timespec="seconds"),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
        )
    return token


def login(email: Any, password: Any):
    """Return (user, session_token). Raises AuthError with a deliberately generic message."""
    if not isinstance(email, str) or not isinstance(password, str):
        raise AuthError("Email and password are required.")
    key = email.strip().lower()
    if _throttled(key):
        raise AuthError("Too many failed attempts. Try again in a few minutes.", 429)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (key,)).fetchone()
    ok = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok:
        _record_failure(key)
        raise AuthError("Invalid email or password.", 401)
    _clear_failures(key)
    return public_user(row), create_session(row["id"])


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def user_from_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with db.session() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (_hash_token(token), datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ).fetchone()
    return public_user(row) if row else None


def logout(token: Optional[str]) -> None:
    if token:
        with db.session() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
