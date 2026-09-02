"""AuditService — append-only administrative audit trail (Brief §12).

- Metadata is redacted (secret-like keys masked) before persistence.
- Audit failures must never break the main operation: they are logged, swallowed.
- There is deliberately no update/delete API — the trail is append-only.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, log_with, redact
from app.models.audit import AuditLog

logger = get_logger("qbit.audit")


def _jsonable(metadata: dict) -> dict:
    """Guarantee JSON-serializable metadata (fallback: str(value)) so a bad
    payload can never break the audit write or the business operation."""
    import json

    try:
        json.dumps(metadata)
        return metadata
    except (TypeError, ValueError):
        return json.loads(json.dumps(metadata, default=str))


class AuditService:
    async def log(
        self,
        session: AsyncSession,
        *,
        action: str,
        actor_user_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        metadata: dict | None = None,
        commit: bool = True,
    ) -> None:
        try:
            entry = AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                ip_address=ip_address,
                user_agent=user_agent,
                metadata_json=_jsonable(redact(metadata or {})),
            )
            session.add(entry)
            if commit:
                await session.commit()
            log_with(
                logger, 20, "audit", action=action,
                resource_type=resource_type, resource_id=resource_id,
            )
        except Exception as exc:  # noqa: BLE001 - auditing must not break business flow
            log_with(logger, 40, "Audit write failed", action=action, error=str(exc))
            try:
                await session.rollback()
            except Exception:  # pragma: no cover
                pass

    async def list_entries(
        self,
        session: AsyncSession,
        *,
        action: str | None = None,
        resource_type: str | None = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if action:
            query = query.where(AuditLog.action == action)
        if resource_type:
            query = query.where(AuditLog.resource_type == resource_type)
        rows = await session.execute(query)
        return list(rows.scalars().all())
