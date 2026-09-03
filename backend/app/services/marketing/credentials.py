"""Credential vault service (Phase 6 §2, §4).

Owns the provider_credentials table:
- create/rotate: encrypt the payload with Fernet (key from QBIT_SECRET_KEY)
- resolve: decrypt for OUT-OF-PROCESS provider calls only — the decrypted
  dict flows straight into the provider client and is never logged,
  serialized into responses, or cached on disk
- every public dict is display-safe (hints only; secret-like hints stripped)

The vault is storage-only. It knows nothing about WhatsApp specifics.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_payload, encrypt_payload, secret_tail
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.messaging import ProviderCredentials

logger = get_logger("qbit.marketing.credentials")

#: keys that must never appear inside a payload even if a caller tries
FORBIDDEN_PAYLOAD_KEYS = {"id", "provider", "name", "created_at", "updated_at"}


class CredentialVault:
    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key

    # ------------------------------------------------------------------ write
    async def store(
        self, session: AsyncSession, *,
        name: str, provider: str, payload: dict,
        created_by: uuid.UUID | None = None,
    ) -> ProviderCredentials:
        """Create or rotate the credential row `name` (idempotent upsert).

        Non-secret hints (token tail only) are stored beside the ciphertext so
        the UI can show *something* without ever exposing the secret.
        """
        clean_name = " ".join(str(name or "").split())[:255]
        if not clean_name:
            raise ValidationError("Credential name must not be empty")
        if not payload:
            raise ValidationError("Credential payload must not be empty")
        for key in payload:
            if str(key).lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise ValidationError(f"Refusing to store reserved key '{key}' in credentials")
        provider = (provider or "").strip()[:50]
        if not provider:
            raise ValidationError("Credential provider must not be empty")

        hints = self._build_hints(payload)
        ciphertext = encrypt_payload(self.secret_key, payload)

        row = await session.scalar(
            select(ProviderCredentials).where(ProviderCredentials.name == clean_name)
        )
        now = datetime.now(timezone.utc)
        if row is None:
            row = ProviderCredentials(
                name=clean_name, provider=provider, ciphertext=ciphertext,
                key_version=1, hints=hints, created_by=created_by,
                last_rotated_at=now,
            )
            session.add(row)
        else:
            row.ciphertext = ciphertext
            row.key_version = 1
            row.hints = hints
            row.last_rotated_at = now
            row.updated_at = now
        await session.commit()
        await session.refresh(row)
        # no secret ever reaches logs — only the row name + key ids
        logger.info(
            "credential_stored",
            extra={"extra_fields": {"credential": clean_name, "provider": provider}},
        )
        return row

    async def delete(self, session: AsyncSession, *, name: str) -> None:
        row = await self._get(session, name)
        await session.delete(row)
        await session.commit()
        logger.info(
            "credential_deleted",
            extra={"extra_fields": {"credential": name}},
        )

    # ------------------------------------------------------------------- read
    async def resolve(self, session: AsyncSession, *, name: str | None) -> dict:
        """Decrypt credentials for provider calls. Raises NotFoundError when
        the reference is missing — honest failure, never an empty config."""
        if not name:
            raise NotFoundError("No credential reference configured for this account")
        row = await self._get(session, name)
        payload = decrypt_payload(self.secret_key, row.ciphertext)
        row.last_used_at = datetime.now(timezone.utc)
        await session.commit()
        return payload

    async def get_row(self, session: AsyncSession, *, name: str) -> ProviderCredentials:
        return await self._get(session, name)

    # ---------------------------------------------------------------- helpers
    async def _get(self, session: AsyncSession, name: str) -> ProviderCredentials:
        row = await session.scalar(
            select(ProviderCredentials).where(ProviderCredentials.name == (name or "").strip())
        )
        if row is None:
            raise NotFoundError("Credential reference not found")
        return row

    @staticmethod
    def _build_hints(payload: dict) -> dict:
        """Non-secret display hints: token tails only, never the values."""
        hints: dict[str, str] = {}
        for key, value in payload.items():
            if not isinstance(value, str) or not value:
                continue
            lowered = key.lower()
            if any(marker in lowered for marker in ("token", "secret", "key", "password")):
                hints[f"{key}_tail"] = secret_tail(value)
        return hints
