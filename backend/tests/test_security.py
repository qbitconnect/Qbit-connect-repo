"""Password hashing + token security tests (Brief §9, §25)."""

from __future__ import annotations

import pytest

from app.core.errors import UnauthorizedError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)

SECRET = "unit-test-secret-" + "z" * 40


def test_password_hash_is_argon2id_and_not_plaintext():
    hashed = hash_password("MySecretPass123")
    assert hashed.startswith("$argon2id$")
    assert "MySecretPass123" not in hashed


def test_password_verify_roundtrip():
    hashed = hash_password("CorrectHorse9")
    assert verify_password("CorrectHorse9", hashed) is True
    assert verify_password("wrong-password", hashed) is False
    assert verify_password("", hashed) is False
    assert verify_password("CorrectHorse9", "corrupted-hash") is False


def test_no_rehash_needed_for_fresh_hash():
    hashed = hash_password("AnotherPass1")
    assert password_needs_rehash(hashed) is False


def test_token_roundtrip():
    token, expires_at = create_access_token(subject="user-123", secret_key=SECRET, ttl_minutes=5)
    payload = decode_access_token(token, secret_key=SECRET)
    assert payload["sub"] == "user-123"
    assert payload["type"] == "access"
    assert expires_at is not None


def test_expired_token_rejected():
    token, _ = create_access_token(subject="u", secret_key=SECRET, ttl_minutes=-1)
    with pytest.raises(UnauthorizedError):
        decode_access_token(token, secret_key=SECRET)


def test_tampered_token_rejected():
    token, _ = create_access_token(subject="u", secret_key=SECRET, ttl_minutes=5)
    with pytest.raises(UnauthorizedError):
        decode_access_token(token[:-3] + "abc", secret_key=SECRET)


def test_wrong_secret_rejected():
    token, _ = create_access_token(subject="u", secret_key=SECRET, ttl_minutes=5)
    with pytest.raises(UnauthorizedError):
        decode_access_token(token, secret_key="different-secret-" + "y" * 40)
