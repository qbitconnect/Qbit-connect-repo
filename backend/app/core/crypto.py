"""Provider credential encryption (Phase 6 §4 — secret management).

The vault encrypts provider secrets at rest with Fernet (AES-128-CBC +
HMAC-SHA256). The key is derived from the platform's existing QBIT_SECRET_KEY
via HKDF-SHA256 with a dedicated salt/info — no new secret source is
introduced, no key material is stored anywhere (only `key_version`).

Guarantees:
- ciphertext in the database is useless without QBIT_SECRET_KEY
- rotating QBIT_SECRET_KEY invalidates stored ciphertexts loudly (InvalidToken)
  — re-entering credentials is the documented recovery, never silent garbage
- no plaintext secret is ever returned by this module's callers to any API
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.errors import ValidationError

#: HKDF parameters — stable constants; changing them breaks decryption of
#: existing vault rows, so they are versioned via `key_version` instead.
_HKDF_SALT = b"qbit-connect/provider-credentials/v1"
_HKDF_INFO = b"qbit-connect provider credential encryption key"
_KEY_VERSION = 1


class SecretVaultError(ValidationError):
    """Vault-level failure (bad ciphertext / bad key). Never carries secrets."""


def derive_vault_key(secret_key: str) -> bytes:
    """Derive the Fernet key from QBIT_SECRET_KEY (HKDF-SHA256, fixed salt).

    The dev-default secret is deliberately accepted here so tests and dev
    environments work; production boot already rejects the dev default in
    Settings.validate_runtime().
    """
    if not secret_key:
        raise SecretVaultError("QBIT_SECRET_KEY is empty — cannot derive vault key")
    kdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    )
    raw = kdf.derive(secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def encrypt_payload(secret_key: str, payload: dict[str, Any]) -> str:
    """Encrypt a JSON-serializable dict → Fernet token (url-safe base64 str)."""
    try:
        blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SecretVaultError("Credential payload is not JSON-serializable") from exc
    return Fernet(derive_vault_key(secret_key)).encrypt(blob).decode("ascii")


def decrypt_payload(secret_key: str, token: str) -> dict[str, Any]:
    """Decrypt a Fernet token back into the payload dict.

    Raises SecretVaultError (a ValidationError subclass) on tampering or key
    mismatch — the error message NEVER contains payload material.
    """
    if not token:
        raise SecretVaultError("Credential ciphertext is empty")
    try:
        blob = Fernet(derive_vault_key(secret_key)).decrypt(token.encode("ascii"))
    except (InvalidToken, binascii.Error, ValueError) as exc:
        raise SecretVaultError(
            "Credential decryption failed — the encryption key does not match "
            "(key_version 1) or the ciphertext was tampered with. Re-enter the "
            "provider credentials."
        ) from exc
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretVaultError("Credential payload is corrupt") from exc
    if not isinstance(payload, dict):
        raise SecretVaultError("Credential payload has an unexpected shape")
    return payload


def secret_tail(value: str | None, visible: int = 4) -> str:
    """Masked hint for display (e.g. '••••abcd') — reveals only the tail.

    Tail-only display is standard practice for rotating secrets; it exposes
    nothing usable for authentication.
    """
    value = str(value or "")
    if not value:
        return ""
    visible = max(0, min(visible, len(value) - 1))
    return "\u2022" * 6 + value[-visible:]


def mask_phone(identifier: str | None) -> str:
    """Mask a phone identifier for display: keep country code + last 3 digits.

    '4915112345678' → '+49•••••••678'. Purely cosmetic — used in APIs/UI so
    full numbers are not broadcast to every viewer (§4).
    """
    raw = "".join(ch for ch in str(identifier or "") if ch.isdigit())
    if len(raw) < 6:
        return "\u2022\u2022\u2022"
    return f"+{raw[:2]}\u2022\u2022\u2022\u2022\u2022{raw[-3:]}"
