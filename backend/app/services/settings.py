"""SystemSettingsService (Brief §11).

Ordinary settings only. Sensitive provider secrets NEVER go here — they will use
the encrypted credential vault (architecture doc 17, later phase).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEFAULT_SETTINGS
from app.core.errors import NotFoundError
from app.core.logging import SECRET_KEYS, redact
from app.models.setting import SystemSetting
from app.services.audit import AuditService


class SystemSettingsService:
    def __init__(self, audit: AuditService) -> None:
        self.audit = audit

    async def get_all(self, session: AsyncSession) -> dict[str, object]:
        rows = await session.execute(select(SystemSetting))
        values: dict[str, object] = {}
        for row in rows.scalars():
            values[row.key] = row.value
        # Merge defaults for not-yet-persisted keys (read-only view).
        for key, raw in DEFAULT_SETTINGS.items():
            values.setdefault(key, self._coerce(raw))
        return values

    async def get(self, session: AsyncSession, key: str) -> object:
        row = await session.scalar(select(SystemSetting).where(SystemSetting.key == key))
        if row is not None:
            return row.value
        if key in DEFAULT_SETTINGS:
            return self._coerce(DEFAULT_SETTINGS[key])
        raise NotFoundError(f"Unknown setting: {key}")

    async def set(
        self,
        session: AsyncSession,
        key: str,
        value: object,
        *,
        updated_by: uuid.UUID | None,
    ) -> SystemSetting:
        if any(secret in key.lower() for secret in SECRET_KEYS):
            raise NotFoundError(
                "Sensitive values must not be stored in system_settings; use the credential vault"
            )
        row = await session.scalar(select(SystemSetting).where(SystemSetting.key == key))
        if row is None:
            row = SystemSetting(key=key, value=value, updated_by=updated_by)
            session.add(row)
        else:
            row.value = value
            row.updated_by = updated_by
        await session.commit()
        await session.refresh(row)
        await self.audit.log(
            session,
            action="settings.updated",
            resource_type="system_setting",
            resource_id=key,
            actor_user_id=updated_by,
            metadata={"key": key},
        )
        return row

    @staticmethod
    def _coerce(raw: str) -> object:
        lowered = raw.lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            import json

            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
