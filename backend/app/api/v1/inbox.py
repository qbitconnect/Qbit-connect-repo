"""Unified Inbox API (Phase 8 §45, §46, §49–§52).

All routes are JWT-authenticated and enforce `inbox.*` permissions
SERVER-SIDE (§49) plus backend visibility scoping (§50). Responses are
scoped: no provider tokens, no credential references, no internal secrets
(§52). Message bodies are displayed from stored data; email HTML is
sanitized at render time by the UI layer (§44).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select as _select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditDep, DbSession, require_permission
from app.core.config import Settings
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.models.messaging import Conversation
from app.services.audit import AuditService
from app.services.inbox.engine import ConversationEngine
from app.services.inbox.reply import ReplyService
from app.services.inbox.workspace import InboxWorkspace

router = APIRouter(prefix="/inbox", tags=["inbox"])

workspace = InboxWorkspace()
engine = ConversationEngine()
replies = ReplyService(engine)


def _settings(request: Request) -> Settings:
    """Settings via app.state DI (test-isolated; matches the rest of the API)."""
    return request.app.state.settings


# ------------------------------------------------------------------ schemas
class ReplyIn(BaseModel):
    body: str | None = Field(default=None, max_length=20000)
    subject: str | None = Field(default=None, max_length=300)
    client_message_id: str = Field(min_length=1, max_length=300)
    template_id: uuid.UUID | None = None


class StatusIn(BaseModel):
    status: str = Field(min_length=1, max_length=20)


class PriorityIn(BaseModel):
    priority: str | None = None


class AssignIn(BaseModel):
    assigned_user_id: uuid.UUID | None = None


class NoteIn(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class LinkLeadIn(BaseModel):
    lead_id: uuid.UUID


class BulkIn(BaseModel):
    conversation_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    action: str  # read | unread | assign | status | priority
    value: str | None = None


# ------------------------------------------------------------------ helpers
def _page_envelope(items: list, total: int, page: int, page_size: int) -> dict:
    total_pages = (total + page_size - 1) // page_size if page_size else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(total_pages, 1),
    }


def _perms(request: Request) -> set[str]:
    """Permissions cached by require_permission on this request (§49)."""
    return getattr(request.state, "permissions", None) or set()


async def _load_conversation(
    session: AsyncSession, conversation_id: uuid.UUID,
) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise NotFoundError("Conversation not found")
    return conversation


async def _ensure_visible(
    session: AsyncSession, conversation: Conversation, request: Request, settings,
) -> None:
    """§50: enforce visibility server-side (frontend filtering is NOT security)."""
    clause = workspace._visibility_clause(
        user_id=request.state.user.id, permissions=_perms(request), settings=settings,
    )
    if clause is None:
        return
    check = await session.execute(
        _select(Conversation.id)
        .where(Conversation.id == conversation.id, clause)
        .limit(1)
    )
    if check.first() is None:
        raise NotFoundError("Conversation not found")


def _require_channel_reply_permission(request: Request, conversation: Conversation) -> None:
    """inbox.reply AND the channel-specific permission (§49)."""
    code = (
        "inbox.whatsapp.reply"
        if conversation.channel == "WHATSAPP"
        else "inbox.email.reply"
    )
    if code not in _perms(request):
        raise PermissionDeniedError(f"Missing required permission: {code}")


def _date_param(value: str | None, *, end: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        pass
    # convenience date forms: today | yesterday | 7d
    term = value.strip().lower()
    now = datetime.now(timezone.utc)
    if term == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start if not end else start + timedelta(days=1)
    if term == "yesterday":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start if not end else start + timedelta(days=1)
    if term == "7d":
        return now - timedelta(days=7) if not end else now
    raise ValidationError(f"Invalid date filter '{value}'")


# ------------------------------------------------------------------- routes
@router.get("/conversations")
async def list_conversations(
    request: Request,
    session: DbSession,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.view")),
    channel: str | None = Query(default=None),
    status: str | None = Query(default=None),
    assignment: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    unread: bool = Query(default=False),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    lead_tag: str | None = Query(default=None),
    lead_status: str | None = Query(default=None),
    lead_source: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
):
    items, total = await workspace.list_conversations(
        session,
        user_id=user.id, permissions=_perms(request), settings=settings,
        channel=channel, status=status, assignment=assignment, priority=priority,
        unread_only=unread,
        date_from=_date_param(date_from), date_to=_date_param(date_to, end=True),
        lead_tag=lead_tag, lead_status=lead_status, lead_source=lead_source,
        search=search, page=page, page_size=page_size,
    )
    return {"success": True, "data": _page_envelope(items, total, page, page_size)}


@router.get("/search")
async def search(
    request: Request,
    session: DbSession,
    q: str = Query(min_length=1, max_length=200),
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.view")),
    channel: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Server-side search across messages (§11, §47) — never client-side."""
    results = await workspace.search_messages(
        session, query_text=q, user_id=user.id,
        permissions=_perms(request), settings=settings,
        channel=channel, limit=limit,
    )
    return {"success": True, "data": {"items": results, "total": len(results)}}


@router.get("/unread-count")
async def unread_count(
    request: Request,
    session: DbSession,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.view")),
):
    counters = await workspace.unread_counters(
        session, user_id=user.id,
        permissions=_perms(request), settings=settings,
    )
    return {"success": True, "data": counters}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    request: Request,
    session: DbSession,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.view")),
):
    data = await workspace.get_conversation(
        session, conversation_id, user_id=user.id,
        permissions=_perms(request), settings=settings,
    )
    return {"success": True, "data": data}


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID,
    request: Request,
    session: DbSession,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.view")),
    before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    await workspace.get_conversation(
        session, conversation_id, user_id=user.id,
        permissions=_perms(request), settings=settings,
    )  # 404s invisible conversations (§50)
    items, next_before = await workspace.list_messages(
        session, conversation_id, before=before, limit=limit,
    )
    return {
        "success": True,
        "data": {
            "items": items,
            "next_before": next_before.isoformat() if next_before else None,
        },
    }


@router.post("/conversations/{conversation_id}/messages", status_code=202)
async def send_reply(
    conversation_id: uuid.UUID,
    payload: ReplyIn,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.reply")),
):
    """Queue a reply (§24). Channel-specific permission enforced (§49)."""
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    _require_channel_reply_permission(request, conversation)
    message, created = await replies.queue_reply(
        session, conversation,
        user_id=user.id, body=payload.body, subject=payload.subject,
        client_message_id=payload.client_message_id,
        template_id=payload.template_id,
        settings=settings,
    )
    if created:
        await audit.log(
            session, action="inbox.message_sent", actor_user_id=user.id,
            resource_type="conversation", resource_id=str(conversation.id),
            metadata={"message_id": str(message.id), "channel": conversation.channel},
        )
    return {"success": True, "data": {"message": message.to_public_dict(), "created": created}}


@router.post("/conversations/{conversation_id}/messages/{message_id}/retry", status_code=202)
async def retry_reply(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.reply")),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    _require_channel_reply_permission(request, conversation)
    from app.models.messaging import Message

    message = await session.get(Message, message_id)
    if message is None or message.conversation_id != conversation_id:
        raise NotFoundError("Message not found")
    message = await replies.retry_failed(
        session, conversation=conversation, message=message, settings=settings,
    )
    await audit.log(
        session, action="inbox.message_retry", actor_user_id=user.id,
        resource_type="conversation", resource_id=str(conversation.id),
        metadata={"message_id": str(message.id)},
    )
    return {"success": True, "data": {"message": message.to_public_dict()}}


@router.post("/conversations/{conversation_id}/read")
async def mark_read(
    conversation_id: uuid.UUID,
    request: Request,
    session: DbSession,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.view")),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    await workspace.mark_read(session, conversation)
    return {"success": True, "data": {"unread_count": 0}}


@router.post("/conversations/{conversation_id}/unread")
async def mark_unread(
    conversation_id: uuid.UUID,
    request: Request,
    session: DbSession,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.view")),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    await workspace.mark_unread(session, conversation)
    return {"success": True, "data": {"unread_count": conversation.unread_count}}


@router.patch("/conversations/{conversation_id}/status")
async def change_status(
    conversation_id: uuid.UUID,
    payload: StatusIn,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.change_status")),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    await engine.change_status(session, conversation, payload.status, actor_user_id=user.id)
    await audit.log(
        session, action="inbox.status_changed", actor_user_id=user.id,
        resource_type="conversation", resource_id=str(conversation.id),
        metadata={"status": payload.status},
    )
    return {"success": True, "data": conversation.to_public_dict()}


@router.patch("/conversations/{conversation_id}/priority")
async def change_priority(
    conversation_id: uuid.UUID,
    payload: PriorityIn,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.change_priority")),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    await engine.change_priority(
        session, conversation, payload.priority, actor_user_id=user.id,
    )
    await audit.log(
        session, action="inbox.priority_changed", actor_user_id=user.id,
        resource_type="conversation", resource_id=str(conversation.id),
        metadata={"priority": payload.priority or "NORMAL"},
    )
    return {"success": True, "data": conversation.to_public_dict()}


@router.post("/conversations/{conversation_id}/assign")
async def assign(
    conversation_id: uuid.UUID,
    payload: AssignIn,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.assign")),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    await engine.assign_user(
        session, conversation, payload.assigned_user_id, actor_user_id=user.id,
    )
    await audit.log(
        session, action="inbox.assigned", actor_user_id=user.id,
        resource_type="conversation", resource_id=str(conversation.id),
        metadata={"assigned_user_id": str(payload.assigned_user_id) if payload.assigned_user_id else None},
    )
    return {"success": True, "data": conversation.to_public_dict()}


@router.post("/conversations/{conversation_id}/notes")
async def add_note(
    conversation_id: uuid.UUID,
    payload: NoteIn,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.add_notes")),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    note = await engine.add_note(session, conversation, payload.content, user_id=user.id)
    await audit.log(
        session, action="inbox.note_added", actor_user_id=user.id,
        resource_type="conversation", resource_id=str(conversation.id),
        metadata={"note_id": str(note.id)},
    )
    return {"success": True, "data": note.to_public_dict()}


@router.get("/conversations/{conversation_id}/activity")
async def activity(
    conversation_id: uuid.UUID,
    request: Request,
    session: DbSession,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.view")),
    limit: int = Query(default=200, ge=1, le=500),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    entries = await workspace.list_activity(session, conversation_id, limit=limit)
    return {"success": True, "data": {"items": entries}}


@router.post("/conversations/{conversation_id}/link-lead")
async def link_lead(
    conversation_id: uuid.UUID,
    payload: LinkLeadIn,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.link_lead")),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    await engine.link_lead(
        session, conversation, payload.lead_id, actor_user_id=user.id,
    )
    await audit.log(
        session, action="inbox.lead_linked", actor_user_id=user.id,
        resource_type="conversation", resource_id=str(conversation.id),
        metadata={"lead_id": str(payload.lead_id)},
    )
    return {"success": True, "data": conversation.to_public_dict()}


@router.post("/conversations/{conversation_id}/unlink-lead")
async def unlink_lead(
    conversation_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.link_lead")),
):
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    await engine.unlink_lead(session, conversation, actor_user_id=user.id)
    await audit.log(
        session, action="inbox.lead_unlinked", actor_user_id=user.id,
        resource_type="conversation", resource_id=str(conversation.id),
    )
    return {"success": True, "data": conversation.to_public_dict()}


@router.post("/conversations/{conversation_id}/create-lead")
async def create_lead(
    conversation_id: uuid.UUID,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.create_lead")),
    display_name: str | None = Query(default=None, max_length=300),
):
    """Create a lead from an unresolved contact (§7) — provider-received
    data only; nothing is invented."""
    conversation = await _load_conversation(session, conversation_id)
    await _ensure_visible(session, conversation, request, settings)
    lead = await engine.create_lead_from_conversation(
        session, conversation, actor_user_id=user.id, display_name=display_name,
    )
    await audit.log(
        session, action="inbox.lead_created", actor_user_id=user.id,
        resource_type="conversation", resource_id=str(conversation.id),
        metadata={"lead_id": str(lead.id)},
    )
    return {"success": True, "data": {"lead_id": str(lead.id),
                                      "conversation": conversation.to_public_dict()}}


@router.post("/bulk")
async def bulk_actions(
    payload: BulkIn,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    settings: Settings = Depends(_settings),
    user=Depends(require_permission("inbox.view")),
):
    """Bulk Mark Read/Unread/Assign/Status/Priority (§46). No bulk delete —
    conversation history is preserved."""
    action = payload.action.strip().lower()
    if action in ("read", "unread"):
        changed = await workspace.bulk_read_state(
            session, conversation_ids=payload.conversation_ids,
            unread=(action == "unread"), user_id=user.id,
            permissions=_perms(request), settings=settings,
        )
        await audit.log(
            session, action=f"inbox.bulk_{action}", actor_user_id=user.id,
            resource_type="conversation", metadata={"count": changed},
        )
        return {"success": True, "data": {"changed": changed}}

    permission_map = {
        "assign": "inbox.assign",
        "status": "inbox.change_status",
        "priority": "inbox.change_priority",
    }
    required = permission_map.get(action)
    if required is None:
        raise ValidationError(f"Unknown bulk action '{payload.action}'")
    if required not in _perms(request):
        raise PermissionDeniedError(f"Missing required permission: {required}")

    value = payload.value
    changed = 0
    for cid in payload.conversation_ids[:500]:
        conversation = await session.get(Conversation, cid)
        if conversation is None:
            continue
        try:
            await _ensure_visible(session, conversation, request, settings)
        except NotFoundError:
            continue
        try:
            if action == "assign":
                target = uuid.UUID(value) if value else None
                await engine.assign_user(
                    session, conversation, target, actor_user_id=user.id,
                )
            elif action == "status":
                await engine.change_status(
                    session, conversation, value or "", actor_user_id=user.id,
                )
            else:
                await engine.change_priority(
                    session, conversation, value, actor_user_id=user.id,
                )
            changed += 1
        except ValidationError:
            continue
    await audit.log(
        session, action=f"inbox.bulk_{action}", actor_user_id=user.id,
        resource_type="conversation", metadata={"count": changed},
    )
    return {"success": True, "data": {"changed": changed}}
