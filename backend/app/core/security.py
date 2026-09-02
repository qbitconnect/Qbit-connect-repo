"""Security primitives: password hashing (argon2id) and access tokens (HS256 JWT).

Brief §9/§25: passwords MUST use a secure hashing algorithm; never plaintext.
Brief §41: secrets stay server-side; the signing key comes from configuration only.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.errors import AuthFailedError, UnauthorizedError

_ph = PasswordHasher()  # argon2id with sane defaults (memory/time tuned by lib)


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time verification. Returns False on any mismatch/corruption."""
    try:
        return _ph.verify(hashed, plain)
    except Exception:  # noqa: BLE001 - any hash/verify failure means "no match"
        return False


def password_needs_rehash(hashed: str) -> bool:
    try:
        return _ph.check_needs_rehash(hashed)
    except (InvalidHashError, ValueError):
        return False


def generate_password(length: int = 16) -> str:
    """Cryptographically random password generator (for admin resets)."""
    return secrets.token_urlsafe(max(length, 12))


def _secret(secret_key: str) -> str:
    if not secret_key:
        raise AuthFailedError("Server signing key is not configured")
    return secret_key


def create_access_token(
    *, subject: str, secret_key: str, ttl_minutes: int, algorithm: str = "HS256"
) -> tuple[str, datetime]:
    """Return (token, expires_at). Tokens are stateless until the auth phase
    upgrades to server-side sessions (documented in docs/25)."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=ttl_minutes)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": expires_at,
        "jti": uuid.uuid4().hex,
        "type": "access",
    }
    token = jwt.encode(payload, _secret(secret_key), algorithm=algorithm)
    return token, expires_at


def decode_access_token(token: str, *, secret_key: str, algorithm: str = "HS256") -> dict:
    try:
        payload = jwt.decode(token, _secret(secret_key), algorithms=[algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid authentication token") from exc
    if payload.get("type") != "access":
        raise UnauthorizedError("Invalid token type")
    return payload
