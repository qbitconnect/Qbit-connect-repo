"""Secret vault tests (Phase 7 §6): encryption at rest, key isolation."""

import pytest

from app.core.errors import ValidationError
from app.services.marketing.secrets import SecretVault

KEY_A = "test-secret-key-" + "a" * 48
KEY_B = "test-secret-key-" + "b" * 48


def test_roundtrip():
    vault = SecretVault(KEY_A)
    ciphertext = vault.encrypt({"smtp_password": "hunter2", "api_key": "sk-123"})
    assert "hunter2" not in ciphertext
    assert vault.decrypt(ciphertext) == {"smtp_password": "hunter2", "api_key": "sk-123"}


def test_ciphertext_not_plaintext():
    vault = SecretVault(KEY_A)
    ciphertext = vault.encrypt({"password": "super-secret-value"})
    assert "super-secret-value" not in ciphertext
    assert len(ciphertext) > 20


def test_wrong_key_rejected():
    vault = SecretVault(KEY_A)
    ciphertext = vault.encrypt({"password": "x"})
    other = SecretVault(KEY_B)
    with pytest.raises(ValidationError):
        other.decrypt(ciphertext)


def test_two_payloads_differ():
    vault = SecretVault(KEY_A)
    # Fernet includes a random IV — identical plaintexts must differ
    assert vault.encrypt({"a": 1}) != vault.encrypt({"a": 1})
