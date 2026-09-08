"""Reusable in-app notification emitter (Phase 11 §25).

Future channels (email / WhatsApp / push) subscribe to these emit points.
Emitting must NEVER break the caller: failures are logged, never raised.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enterprise import Notification

logger = get_logger("qbit.notifications")


async def emit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None,
    type: str,
    title: str,
    body: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    commit: bool = False,
) -> Notification | None:
    """Create one in-app notification row. Caller owns the commit unless
    commit=True (used from background workers)."""
    try:
        row = Notification(
            user_id=user_id,
            organization_id=organization_id,
            type=type,
            title=title[:300],
            body=body,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        session.add(row)
        await session.flush()
        if commit:
            await session.commit()
        return row
    except Exception:  # noqa: BLE001 — notifications must never break callers
        logger.exception("Failed to emit notification", extra={"type": type})
        return None
