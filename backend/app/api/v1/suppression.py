"""Suppression list + opt-out API (Phase 5 §14, §15, §29).

Suppressed contacts never enter the send queue — the eligibility engine and
the pre-send re-check both consult the data maintained here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditDep, DbSession, require_permission
from app.core.errors import ValidationError
from app.schemas.marketing import OptOutCreate, SuppressionCreate
from app.services.marketing import SuppressionService

router = APIRouter(prefix="/suppression-list", tags=["suppression"])

suppression_service = SuppressionService()


def _page(items: list, total: int, page: int, page_size: int) -> dict:
    return {
        "success": True,
        "data": {
            "items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": max(1, -(-total // page_size)) if total else 1,
        },
    }


def _uuid_or_none(raw: str | None) -> uuid.UUID | None:
    if raw in (None, ""):
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Invalid UUID: {raw!r}") from exc


@router.get("")
async def list_suppression(
    session: DbSession,
    _user=Depends(require_permission("suppression.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    type: str | None = Query(default=None, max_length=20),
    channel: str | None = Query(default=None, max_length=20),
    search: str = Query(default="", max_length=200),
):
    rows, total = await suppression_service.list_entries(
        session, entry_type=type, channel=channel, search=search or None,
        page=page, page_size=page_size,
    )
    return _page([r.to_public_dict() for r in rows], total, page, page_size)


@router.post("", status_code=201)
async def add_suppression(
    payload: SuppressionCreate,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("suppression.manage")),
):
    entry = await suppression_service.add(
        session,
        entry_type=payload.type, address=payload.address, reason=payload.reason,
        channel=payload.channel, source=payload.source,
        lead_id=_uuid_or_none(payload.lead_id), created_by=user.id,
    )
    await audit.log(session, action="suppression.added", resource_type="suppression_entry",
                    resource_id=str(entry.id), actor_user_id=user.id,
                    metadata={"type": entry.type, "reason": entry.reason})
    return {"success": True, "data": entry.to_public_dict()}


@router.delete("/{entry_id}")
async def remove_suppression(
    entry_id: uuid.UUID,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("suppression.manage")),
):
    """Opt-out-backed entries are refused — never silently re-enable (§15)."""
    await suppression_service.remove(session, entry_id)
    await audit.log(session, action="suppression.removed", resource_type="suppression_entry",
                    resource_id=str(entry_id), actor_user_id=user.id)
    return {"success": True, "data": {"deleted": True}}


@router.get("/opt-outs")
async def list_opt_outs(
    session: DbSession,
    _user=Depends(require_permission("suppression.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    channel: str | None = Query(default=None, max_length=20),
):
    rows, total = await suppression_service.list_opt_outs(
        session, channel=channel, page=page, page_size=page_size,
    )
    return _page([r.to_public_dict() for r in rows], total, page, page_size)


@router.post("/opt-outs", status_code=201)
async def record_opt_out(
    payload: OptOutCreate,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("suppression.manage")),
):
    """Record an opt-out/unsubscribe. ALWAYS creates the matching suppression
    entry — opted-out contacts are immediately unsendable (§15)."""
    record = await suppression_service.record_opt_out(
        session,
        channel=payload.channel, address=payload.address, reason=payload.reason,
        source=payload.source or "manual", lead_id=_uuid_or_none(payload.lead_id),
    )
    await audit.log(session, action="suppression.opt_out_recorded",
                    resource_type="opt_out_record", resource_id=str(record.id),
                    actor_user_id=user.id, metadata={"channel": record.channel})
    return {"success": True, "data": record.to_public_dict()}
