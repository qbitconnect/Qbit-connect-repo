"""In-app notifications (Phase 11 §25).

Reusable foundation: services/notifications.py emit_* helpers create rows; the
API exposes list/unread-count/mark-read. Future channels (email/WhatsApp/push)
hook into the same emit points without API changes.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, require_permission
from app.models.enterprise import Notification
from app.schemas.common import PageMeta
from app.schemas.enterprise import NotificationListOut, NotificationOut

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListOut)
async def list_notifications(
    session: DbSession,
    user: CurrentUser,
    unread_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _perm: None = Depends(require_permission("notifications.view")),
):
    base = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        base = base.where(Notification.read_at.is_(None))
    total = int(
        await session.scalar(
            select(func.count()).select_from(Notification)
            .where(
                Notification.user_id == user.id,
                *( [Notification.read_at.is_(None)] if unread_only else [] ),
            )
        )
        or 0
    )
    rows = (
        (await session.execute(base.order_by(Notification.created_at.desc())
                               .offset((page - 1) * page_size).limit(page_size)))
        .scalars().all()
    )
    return NotificationListOut(
        data=[
            NotificationOut(
                id=str(n.id), type=n.type, title=n.title, body=n.body,
                resource_type=n.resource_type, resource_id=n.resource_id,
                read_at=n.read_at, created_at=n.created_at,
            )
            for n in rows
        ],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


@router.get("/unread-count")
async def unread_count(
    session: DbSession,
    user: CurrentUser,
    _perm: None = Depends(require_permission("notifications.view")),
):
    count = int(
        await session.scalar(
            select(func.count()).select_from(Notification).where(
                Notification.user_id == user.id, Notification.read_at.is_(None)
            )
        )
        or 0
    )
    return {"success": True, "data": {"unread": count}}


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _perm: None = Depends(require_permission("notifications.view")),
):
    from datetime import datetime, timezone

    row = await session.get(Notification, notification_id)
    if row is None or row.user_id != user.id:
        from app.core.errors import NotFoundError

        raise NotFoundError("Notification not found")
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        await session.commit()
    return {"success": True, "data": {"id": str(row.id), "read": True}}
