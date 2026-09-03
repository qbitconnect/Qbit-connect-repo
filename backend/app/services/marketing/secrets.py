"""Encrypted-at-rest provider credential vault (Phase 7 §4–§6).

- Fernet (AES-128-CBC + HMAC) encryption; the key is derived from
  QBIT_SECRET_KEY via HKDF-SHA256 with a fixed info label, so rotating the
  platform secret invalidates old ciphertexts deliberately (re-enter creds).
- The plaintext NEVER leaves this module's call sites: callers store the
  returned ``ref`` on the sending account and re-fetch secrets only when
  talking to the provider.
- API responses NEVER include secret values; UI masks them; logs redact them.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.models.marketing import SecretVaultEntry

_VAULT_INFO = b"qbit-connect:marketing:secret-vault:v1"
_FERNET_CACHE: dict[str, Fernet] = {}


def _fernet(secret_key: str) -> Fernet:
    cached = _FERNET_CACHE.get(secret_key)
    if cached is not None:
        return cached
    kdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_VAULT_INFO)
    key = base64.urlsafe_b64encode(kdf.derive(secret_key.encode("utf-8")))
    f = Fernet(key)
    _FERNET_CACHE[secret_key] = f
    return f


def new_credential_ref(channel: str, account_name: str) -> str:
    """Non-secret, non-guessable vault reference for an account."""
    slug = "".join(c for c in account_name.lower() if c.isalnum() or c == "-")[:40] or "acct"
    return f"{channel.lower()}:{slug}:{uuid.uuid4().hex[:12]}"


class SecretVault:
    """Store/retrieve credential dicts, encrypted at rest."""

    def __init__(self, secret_key: str) -> None:
        self._f = _fernet(secret_key)

    def encrypt(self, payload: dict[str, Any]) -> str:
        import json

        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self._f.encrypt(raw).decode("ascii")

    def decrypt(self, ciphertext: str) -> dict[str, Any]:
        import json

        try:
            raw = self._f.decrypt(ciphertext.encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            raise ValidationError(
                "Stored credentials cannot be decrypted with the current "
                "QBIT_SECRET_KEY. Re-enter the account credentials."
            ) from exc
        return json.loads(raw.decode("utf-8"))

    async def put(
        self,
        session: AsyncSession,
        *,
        ref: str,
        payload: dict[str, Any],
        description: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> str:
        """Create or rotate the entry for ``ref``. Returns the ref."""
        ciphertext = self.encrypt(payload)
        row = await session.scalar(select(SecretVaultEntry).where(SecretVaultEntry.ref == ref))
        if row is None:
            row = SecretVaultEntry(
                ref=ref, ciphertext=ciphertext, description=description, created_by=created_by
            )
            session.add(row)
        else:
            row.ciphertext = ciphertext
            if description is not None:
                row.description = description
        await session.flush()
        return ref

    async def get(self, session: AsyncSession, ref: str) -> dict[str, Any] | None:
        row = await session.scalar(select(SecretVaultEntry).where(SecretVaultEntry.ref == ref))
        if row is None:
            return None
        return self.decrypt(row.ciphertext)

    async def delete(self, session: AsyncSession, ref: str) -> bool:
        row = await session.scalar(select(SecretVaultEntry).where(SecretVaultEntry.ref == ref))
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True


def get_vault(secret_key: str) -> SecretVault:
    return SecretVault(secret_key)
