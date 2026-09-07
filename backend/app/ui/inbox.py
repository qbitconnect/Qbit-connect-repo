"""Unified Inbox UI (Phase 8 §9–§13, §16–§33, §57–§60).

Server-rendered three-pane workspace (conversations / thread / lead context)
in the established QBIT dark theme. Cookie-session auth reusing the same
service layer as the REST API; permissions enforced here too (the UI is
NEVER the security boundary).

Realtime strategy (§37, §60): lightweight polling (counters every 12s; open
thread every 8s) — the project's existing realtime mechanism — with an
append-check so reconnects never duplicate messages.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import Settings
from app.core.errors import NotFoundError, QBITError
from app.models.marketing import CampaignTemplate, ProviderTemplateStatus, TemplateOrigin
from app.models.messaging import Conversation
from app.models.user import User
from app.services.inbox.engine import ConversationEngine
from app.services.inbox.reply import ReplyService
from app.services.inbox.workspace import InboxWorkspace
from app.ui import _ctx, templates
from app.ui import ui_user_for as require_ui_permission

router = APIRouter(tags=["inbox-ui"])

workspace = InboxWorkspace()
engine = ConversationEngine()
replies = ReplyService(engine)


def _perms(request: Request) -> set[str]:
    return getattr(request.state, "ui_permissions", None) or set()


async def _load_visible_conversation(
    request: Request, session: AsyncSession, conversation_id: uuid.UUID,
    settings: Settings, user: User | None = None,
) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise NotFoundError("Conversation not found")
    # §50 — reuse the API layer's visibility rule (request.state.user is what
    # _ensure_visible reads; UI auth supplies it here)
    if user is not None:
        request.state.user = user
    from app.api.v1.inbox import _ensure_visible

    await _ensure_visible(session, conversation, request, settings)
    return conversation


def _error_response(exc: QBITError) -> JSONResponse:
    return JSONResponse(
        {"success": False, "error": {"code": exc.code, "message": exc.message}},
        status_code=exc.status_code,
    )


# --------------------------------------------------------------------- page
@router.get("/inbox", response_class=HTMLResponse)
async def inbox_page(
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
    conversation_id: uuid.UUID | None = Query(default=None),
):
    settings: Settings = request.app.state.settings
    async with request.app.state.db.session() as session:
        counters = await workspace.unread_counters(
            session, user_id=user.id, permissions=_perms(request), settings=settings,
        )
    return templates.TemplateResponse(
        request, "inbox/index.html",
        _ctx(request, user, counters=counters,
             selected_id=str(conversation_id) if conversation_id else None,
             permissions=_perms(request)),
    )


# ---------------------------------------------------------------- data APIs
@router.get("/ui/inbox/counters")
async def ui_counters(
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
):
    settings: Settings = request.app.state.settings
    async with request.app.state.db.session() as session:
        counters = await workspace.unread_counters(
            session, user_id=user.id, permissions=_perms(request), settings=settings,
        )
    return {"success": True, "data": counters}


@router.get("/ui/inbox/conversations")
async def ui_list_conversations(
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
    channel: str | None = None,
    status: str | None = None,
    assignment: str | None = None,
    priority: str | None = None,
    unread: bool = False,
    search: str | None = None,
    date_from: str | None = None,
    page: int = 1,
    page_size: int = 25,
):
    settings: Settings = request.app.state.settings
    async with request.app.state.db.session() as session:
        from app.api.v1.inbox import _date_param

        try:
            items, total = await workspace.list_conversations(
                session,
                user_id=user.id, permissions=_perms(request), settings=settings,
                channel=channel, status=status, assignment=assignment,
                priority=priority, unread_only=unread, search=search,
                date_from=_date_param(date_from),
                page=max(page, 1), page_size=min(max(page_size, 1), 200),
            )
        except QBITError as exc:
            return _error_response(exc)
    return {"success": True, "data": {"items": items, "total": total,
                                      "page": page, "page_size": page_size}}


@router.get("/ui/inbox/conversations/{conversation_id}")
async def ui_conversation_detail(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
):
    settings: Settings = request.app.state.settings
    try:
        async with request.app.state.db.session() as session:
            conversation = await _load_visible_conversation(
                request, session, conversation_id, settings, user,
            )
            data = await workspace.get_conversation(
                session, conversation_id, user_id=user.id,
                permissions=_perms(request), settings=settings,
            )
            # WhatsApp reply window (§22) — surfaced to the composer honestly
            if conversation.channel == "WHATSAPP":
                data["whatsapp_window_open"] = engine.within_whatsapp_window(
                    conversation, hours=settings.QBIT_INBOX_WHATSAPP_WINDOW_HOURS,
                )
    except QBITError as exc:
        return _error_response(exc)
    return {"success": True, "data": data}


@router.get("/ui/inbox/conversations/{conversation_id}/messages")
async def ui_messages(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
    before: str | None = None,
    limit: int = 50,
):
    settings: Settings = request.app.state.settings
    try:
        async with request.app.state.db.session() as session:
            await _load_visible_conversation(request, session, conversation_id, settings, user)
            parsed = None
            if before:
                try:
                    parsed = datetime.fromisoformat(before.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise NotFoundError("Invalid cursor") from exc
            items, next_before = await workspace.list_messages(
                session, conversation_id, before=parsed, limit=min(max(limit, 1), 200),
            )
    except QBITError as exc:
        return _error_response(exc)
    return {"success": True, "data": {"items": items,
                                      "next_before": next_before.isoformat() if next_before else None}}


@router.get("/ui/inbox/conversations/{conversation_id}/activity")
async def ui_activity(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
):
    try:
        async with request.app.state.db.session() as session:
            entries = await workspace.list_activity(session, conversation_id)
    except QBITError as exc:
        return _error_response(exc)
    return {"success": True, "data": {"items": entries}}


@router.get("/ui/inbox/assignable-users")
async def ui_assignable_users(
    request: Request,
    user: User = Depends(require_ui_permission("inbox.assign")),
):
    async with request.app.state.db.session() as session:
        rows = (await session.execute(
            select(User).where(User.is_active.is_(True)).order_by(User.full_name)
        )).scalars().all()
    return {"success": True, "data": {
        "items": [{"id": str(u.id), "label": u.full_name or u.email} for u in rows]
    }}


@router.get("/ui/inbox/conversations/{conversation_id}/templates")
async def ui_conversation_templates(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
):
    """APPROVED provider templates for this conversation's account (§22)."""
    settings: Settings = request.app.state.settings
    async with request.app.state.db.session() as session:
        conversation = await _load_visible_conversation(request, session, conversation_id, settings, user)
        if conversation.channel != "WHATSAPP" or conversation.sending_account_id is None:
            return {"success": True, "data": {"items": []}}
        rows = (await session.execute(
            select(CampaignTemplate).where(
                CampaignTemplate.account_id == conversation.sending_account_id,
                CampaignTemplate.origin == TemplateOrigin.PROVIDER.value,
                CampaignTemplate.provider_status == ProviderTemplateStatus.APPROVED.value,
            ).order_by(CampaignTemplate.name).limit(100)
        )).scalars().all()
    return {"success": True, "data": {"items": [{
        "id": str(t.id), "name": t.name, "language": t.language,
        "variables": t.variables or [],
    } for t in rows]}}


# ------------------------------------------------------------ action (POST)
def _json_ok(data) -> JSONResponse:
    return JSONResponse({"success": True, "data": data}, status_code=200)


async def _run_action(request: Request, conversation_id: uuid.UUID, action, user):
    """Shared wrapper: open session, load a visible conversation, run, respond."""
    settings: Settings = request.app.state.settings
    async with request.app.state.db.session() as session:
        try:
            conversation = await _load_visible_conversation(
                request, session, conversation_id, settings, user,
            )
            return await action(session, conversation, settings)
        except QBITError as exc:
            return _error_response(exc)


@router.post("/ui/inbox/conversations/{conversation_id}/messages")
async def ui_send_reply(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.reply")),
    body: str = Form(default=""),
    subject: str = Form(default=""),
    client_message_id: str = Form(default=""),
    template_id: str = Form(default=""),
):
    """Queue a reply through the SAME service the API uses (§24)."""
    from app.api.v1.inbox import _require_channel_reply_permission

    settings: Settings = request.app.state.settings
    async with request.app.state.db.session() as session:
        try:
            conversation = await _load_visible_conversation(
                request, session, conversation_id, settings, user,
            )
            _require_channel_reply_permission(request, conversation)
            message, created = await replies.queue_reply(
                session, conversation,
                user_id=user.id,
                body=body or None,
                subject=(subject or None) if conversation.channel == "EMAIL" else None,
                client_message_id=client_message_id or f"ui-{uuid.uuid4().hex}",
                template_id=uuid.UUID(template_id) if template_id else None,
                settings=settings,
            )
            await request.app.state.audit.log(
                session, action="inbox.message_sent", actor_user_id=user.id,
                resource_type="conversation", resource_id=str(conversation.id),
                metadata={"message_id": str(message.id), "via": "ui"},
            )
        except QBITError as exc:
            return _error_response(exc)
    return JSONResponse({"success": True, "data": {
        "message": message.to_public_dict(), "created": created,
    }}, status_code=202)


@router.post("/ui/inbox/conversations/{conversation_id}/read")
async def ui_mark_read(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
):
    async def action(session, conversation, settings):
        await workspace.mark_read(session, conversation)
        return _json_ok({"unread_count": 0})

    return await _run_action(request, conversation_id, action, user)


@router.post("/ui/inbox/conversations/{conversation_id}/unread")
async def ui_mark_unread(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
):
    async def action(session, conversation, settings):
        await workspace.mark_unread(session, conversation)
        return _json_ok({"unread_count": conversation.unread_count})

    return await _run_action(request, conversation_id, action, user)


@router.post("/ui/inbox/conversations/{conversation_id}/status")
async def ui_change_status(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.change_status")),
    status: str = Form(...),
):
    async def action(session, conversation, settings):
        await engine.change_status(session, conversation, status, actor_user_id=user.id)
        return _json_ok(conversation.to_public_dict())

    return await _run_action(request, conversation_id, action, user)


@router.post("/ui/inbox/conversations/{conversation_id}/priority")
async def ui_change_priority(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.change_priority")),
    priority: str = Form(...),
):
    async def action(session, conversation, settings):
        await engine.change_priority(session, conversation, priority or None, actor_user_id=user.id)
        return _json_ok(conversation.to_public_dict())

    return await _run_action(request, conversation_id, action, user)


@router.post("/ui/inbox/conversations/{conversation_id}/assign")
async def ui_assign(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.assign")),
    assigned_user_id: str = Form(default=""),
):
    async def action(session, conversation, settings):
        target = uuid.UUID(assigned_user_id) if assigned_user_id else None
        await engine.assign_user(session, conversation, target, actor_user_id=user.id)
        return _json_ok(conversation.to_public_dict())

    return await _run_action(request, conversation_id, action, user)


@router.post("/ui/inbox/conversations/{conversation_id}/notes")
async def ui_add_note(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.add_notes")),
    content: str = Form(...),
):
    async def action(session, conversation, settings):
        note = await engine.add_note(session, conversation, content, user_id=user.id)
        await request.app.state.audit.log(
            session, action="inbox.note_added", actor_user_id=user.id,
            resource_type="conversation", resource_id=str(conversation.id),
            metadata={"note_id": str(note.id), "via": "ui"},
        )
        return _json_ok(note.to_public_dict())

    return await _run_action(request, conversation_id, action, user)


@router.post("/ui/inbox/conversations/{conversation_id}/link-lead")
async def ui_link_lead(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.link_lead")),
    lead_id: str = Form(...),
):
    async def action(session, conversation, settings):
        try:
            lead_uuid = uuid.UUID(lead_id.strip())
        except ValueError as exc:
            raise NotFoundError("Lead not found") from exc
        await engine.link_lead(session, conversation, lead_uuid, actor_user_id=user.id)
        await request.app.state.audit.log(
            session, action="inbox.lead_linked", actor_user_id=user.id,
            resource_type="conversation", resource_id=str(conversation.id),
            metadata={"lead_id": str(lead_uuid), "via": "ui"},
        )
        return _json_ok(conversation.to_public_dict())

    return await _run_action(request, conversation_id, action, user)


@router.post("/ui/inbox/conversations/{conversation_id}/create-lead")
async def ui_create_lead(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.create_lead")),
    display_name: str = Form(default=""),
):
    async def action(session, conversation, settings):
        lead = await engine.create_lead_from_conversation(
            session, conversation, actor_user_id=user.id,
            display_name=(display_name or "").strip() or None,
        )
        await request.app.state.audit.log(
            session, action="inbox.lead_created", actor_user_id=user.id,
            resource_type="conversation", resource_id=str(conversation.id),
            metadata={"lead_id": str(lead.id), "via": "ui"},
        )
        return _json_ok({"lead_id": str(lead.id),
                         "conversation": conversation.to_public_dict()})

    return await _run_action(request, conversation_id, action, user)


@router.post("/ui/inbox/conversations/{conversation_id}/messages/{message_id}/retry")
async def ui_retry_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.reply")),
):
    from app.api.v1.inbox import _require_channel_reply_permission
    from app.models.messaging import Message

    settings: Settings = request.app.state.settings
    async with request.app.state.db.session() as session:
        try:
            conversation = await _load_visible_conversation(
                request, session, conversation_id, settings, user,
            )
            _require_channel_reply_permission(request, conversation)
            message = await session.get(Message, message_id)
            if message is None or message.conversation_id != conversation.id:
                raise NotFoundError("Message not found")
            message = await replies.retry_failed(
                session, conversation=conversation, message=message, settings=settings,
            )
        except QBITError as exc:
            return _error_response(exc)
    return _json_ok(message.to_public_dict())


@router.get("/ui/inbox/messages/{message_id}/html")
async def ui_message_html(
    message_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_ui_permission("inbox.view")),
):
    """Sanitized email HTML (§44): nh3 allowlist BEFORE it reaches the
    browser; scripts/handlers/javascript: URLs can never execute."""
    from app.models.messaging import Message
    from app.services.marketing.email_compose import sanitize_html

    async with request.app.state.db.session() as session:
        message = await session.get(Message, message_id)
        if message is None:
            raise NotFoundError("Message not found")
        # visibility: message must belong to a conversation the user can see
        settings: Settings = request.app.state.settings
        conversation = await session.get(Conversation, message.conversation_id)
        if conversation is None:
            raise NotFoundError("Message not found")
        await _load_visible_conversation(request, session, conversation.id, settings, user)
        raw = (message.message_metadata or {}).get("html") or message.body or ""
    from fastapi.responses import HTMLResponse

    return HTMLResponse(sanitize_html(str(raw)[:200000]))
