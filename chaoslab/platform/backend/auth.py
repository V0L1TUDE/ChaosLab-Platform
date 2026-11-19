"""
ChaosLab Platform Authentication & Cryptographic Token Manager
==============================================================
Provides constant-time password hashing (PBKDF2-HMAC-SHA256 with 100,000 rounds)
and RFC 7519 compliant JSON Web Token (JWT) generation and validation.
Zero external dependencies: 100% standard library compliance.
"""

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

JWT_SECRET = os.environ.get("CHAOSLAB_JWT_SECRET", "chaoslab-production-super-secret-key-32bytes-min")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_SECONDS = int(os.environ.get("CHAOSLAB_JWT_EXPIRY", "86400"))  # 24 hours

def hash_password(password: str) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with 100,000 rounds and random 16-byte salt."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    dk_b64 = base64.b64encode(dk).decode("ascii")
    return f"pbkdf2:sha256:100000${salt_b64}${dk_b64}"

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Constant-time verification of plain password against PBKDF2 hash."""
    try:
        parts = hashed_password.split("$")
        if len(parts) != 3:
            return False
        algo_rounds, salt_b64, dk_b64 = parts
        _, _, rounds_str = algo_rounds.split(":")
        rounds = int(rounds_str)
        salt = base64.b64decode(salt_b64)
        expected_dk = base64.b64decode(dk_b64)
        computed_dk = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(expected_dk, computed_dk)
    except Exception:
        return False

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def _b64url_decode(data_str: str) -> bytes:
    padding = "=" * (4 - (len(data_str) % 4))
    return base64.urlsafe_b64decode((data_str + padding).encode("ascii"))

def create_jwt_token(payload: Dict[str, Any], expires_in: int = JWT_EXPIRATION_SECONDS) -> str:
    """Create a signed HS256 JWT token with standard claims."""
    now = int(time.time())
    full_payload = {
        **payload,
        "iat": now,
        "exp": now + expires_in
    }
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(full_payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{sig_b64}"

def verify_jwt_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify signature and expiration of JWT token. Returns payload dict or None."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected_sig = hmac.new(JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
        provided_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, provided_sig):
            return None

        payload_bytes = _b64url_decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))

        now = int(time.time())
        if "exp" in payload and now > payload["exp"]:
            return None

        return payload
    except Exception:
        return None

def extract_bearer_token(auth_header: Optional[str]) -> Optional[str]:
    """Parse 'Bearer <token>' from Authorization HTTP header."""
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip()
