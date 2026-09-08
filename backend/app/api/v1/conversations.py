"""Conversation endpoints (Phase 11 §11) — minimal inbox foundation.

The inbox UI is a later phase; this router adds the REQUIRED backend surface:
list conversations (visibility-aware), get one (IDOR-safe), and assignment
(single/bulk/unassign) with full history. Existing webhook-driven behavior is
untouched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.api.deps import DbSession, get_client_ip, require_context
from app.core.errors import ConflictError, NotFoundError
from app.models.enterprise import ConversationAssignmentHistory, Team
from app.models.messaging import Conversation, Message, MessageDirection, MessageStatus
from app.models.user import User
from app.schemas.common import PageMeta
from app.schemas.enterprise import (
    AssignmentIn,
    AssignmentOut,
    BulkAssignmentIn,
    BulkAssignmentOut,
    ConversationListOut,
    ConversationOut,
    MessageListOut,
    MessageOut,
    ReplyIn,
    ReplyOut,
)
from app.services import authorization as authz
from app.services import notifications as notification_service
from app.services.authorization import MemberContext

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _to_out(c: Conversation) -> ConversationOut:
    return ConversationOut(
        id=str(c.id),
        channel=c.channel,
        status=c.status,
        lead_id=str(c.lead_id) if c.lead_id else None,
        sending_account_id=str(c.sending_account_id) if c.sending_account_id else None,
        contact_phone=c.contact_phone,
        contact_email=c.contact_email,
        assigned_user_id=str(c.assigned_user_id) if c.assigned_user_id else None,
        assigned_team_id=str(c.assigned_team_id) if c.assigned_team_id else None,
        last_message_at=c.last_message_at,
        created_at=c.created_at,
    )


@router.get("", response_model=ConversationListOut)
async def list_conversations(
    session: DbSession,
    ctx: MemberContext = Depends(require_context("inbox.view")),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    query = authz.apply_visibility(
        select(Conversation), Conversation, ctx
    )
    if status:
        query = query.where(Conversation.status == status.upper())
    total = int(
        await session.scalar(
            select(func.count()).select_from(Conversation).where(
                Conversation.organization_id == ctx.organization_id
            )
        )
        or 0
    )
    rows = (
        (await session.execute(query.order_by(Conversation.last_message_at.desc().nullslast())
                               .offset((page - 1) * page_size).limit(page_size)))
        .scalars().all()
    )
    return ConversationListOut(
        data=[_to_out(c) for c in rows],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: uuid.UUID,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("inbox.view")),
):
    c = await authz.get_visible_or_404(session, Conversation, conversation_id, ctx)
    return _to_out(c)


@router.post("/{conversation_id}/assignment", response_model=AssignmentOut)
async def assign_conversation(
    conversation_id: uuid.UUID,
    payload: AssignmentIn,
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("inbox.assign")),
):
    c = await authz.get_visible_or_404(session, Conversation, conversation_id, ctx)
    new_user_id = uuid.UUID(payload.assigned_user_id) if payload.assigned_user_id else None
    new_team_id = uuid.UUID(payload.assigned_team_id) if payload.assigned_team_id else None

    if new_user_id is not None:
        target = await session.get(User, new_user_id)
        if target is None:
            raise NotFoundError("Target user not found")
        from app.services.authorization import get_membership

        if await get_membership(session, new_user_id, ctx.organization_id) is None:
            raise NotFoundError("Target user not found")
    if new_team_id is not None:
        team = await session.get(Team, new_team_id)
        if team is None or team.organization_id != ctx.organization_id:
            raise NotFoundError("Target team not found")

    prev_user, prev_team = c.assigned_user_id, c.assigned_team_id
    c.assigned_user_id = new_user_id
    c.assigned_team_id = new_team_id
    history = ConversationAssignmentHistory(
        conversation_id=c.id,
        organization_id=ctx.organization_id,
        previous_user_id=prev_user,
        previous_team_id=prev_team,
        assigned_user_id=new_user_id,
        assigned_team_id=new_team_id,
        changed_by=ctx.user.id,
        reason=payload.reason,
    )
    session.add(history)
    await session.commit()

    if new_user_id is not None and new_user_id != ctx.user.id:
        await notification_service.emit(
            session,
            user_id=new_user_id,
            organization_id=ctx.organization_id,
            type="ASSIGNMENT",
            title="A conversation was assigned to you",
            resource_type="conversation",
            resource_id=str(c.id),
        )
    await request.app.state.audit.log(
        session,
        action="conversation.assigned",
        actor_user_id=ctx.user.id,
        resource_type="conversation",
        resource_id=str(c.id),
        ip_address=get_client_ip(request),
        metadata={
            "previous_user_id": str(prev_user) if prev_user else None,
            "assigned_user_id": str(new_user_id) if new_user_id else None,
            "assigned_team_id": str(new_team_id) if new_team_id else None,
            "reason": payload.reason,
        },
    )
    return AssignmentOut(data={"id": str(c.id), **_to_out(c).model_dump()})


@router.get("/{conversation_id}/messages", response_model=MessageListOut)
async def list_messages(
    conversation_id: uuid.UUID,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("inbox.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    c = await authz.get_visible_or_404(session, Conversation, conversation_id, ctx)
    count_q = (
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == c.id)
    )
    total = int(await session.scalar(count_q) or 0)
    rows = (
        (await session.execute(
            select(Message)
            .where(Message.conversation_id == c.id)
            .order_by(Message.created_at.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )).scalars().all()
    )
    return MessageListOut(
        data=[
            MessageOut(
                id=str(m.id),
                conversation_id=str(m.conversation_id),
                direction=m.direction,
                message_type=m.message_type,
                body=m.body,
                status=m.status,
                provider_message_id=m.provider_message_id,
                metadata=m.message_metadata or {},
                created_at=m.created_at,
            )
            for m in rows
        ],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


@router.post("/{conversation_id}/reply", response_model=ReplyOut)
async def reply_to_conversation(
    conversation_id: uuid.UUID,
    payload: ReplyIn,
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("inbox.reply")),
):
    """Send one outbound message inside an existing conversation.

    Honest delivery: the message goes through the SAME provider stack used by
    campaigns (resolve credentials → provider.send → SendResult). When no
    usable sending account exists the reply is refused (409) — never silently
    'queued' into a void (platform honesty rule).
    """
    c = await authz.get_visible_or_404(session, Conversation, conversation_id, ctx)

    # resolve a usable sending account: prefer the conversation's own account
    from app.models.marketing import AccountHealth, AccountStatus, SendingAccount

    account = None
    if c.sending_account_id:
        candidate = await session.get(SendingAccount, c.sending_account_id)
        if (
            candidate is not None
            and (candidate.organization_id is None or candidate.organization_id == ctx.organization_id)
            and await authz.can_use_connection(session, ctx, candidate)
        ):
            account = candidate
    if account is None:
        rows = (
            (await session.execute(
                select(SendingAccount).where(
                    SendingAccount.channel == c.channel,
                    SendingAccount.status.in_([AccountStatus.ACTIVE.value, "CONNECTED"]),
                )
            )).scalars().all()
        )
        for candidate in rows:
            if candidate.health_status == AccountHealth.UNHEALTHY.value:
                continue
            if candidate.organization_id is not None and candidate.organization_id != ctx.organization_id:
                continue
            if not await authz.can_use_connection(session, ctx, candidate):
                continue
            account = candidate
            break
    if account is None:
        raise ConflictError(
            "No accessible sending account for this conversation's channel; "
            "the reply cannot be delivered"
        )

    # recipient identity — never guess: the conversation must carry one
    recipient_address = c.contact_phone if c.channel == "WHATSAPP" else c.contact_email
    if not recipient_address:
        raise ConflictError("The conversation has no contact identity to reply to")

    registry = request.app.state.marketing_providers
    if registry is None:
        from app.services.marketing.providers import build_provider_registry

        registry = build_provider_registry(request.app.state.settings)
        request.app.state.marketing_providers = registry
    provider = registry.get(account.provider)
    if provider is None:
        raise ConflictError(f"Provider '{account.provider}' is not available")

    # credentials resolved for THIS call only (vault → env), never logged
    if account.channel == "EMAIL":
        from app.services.marketing.connections_email import (
            email_account_config_for,
            resolve_email_credentials,
        )

        credentials = await resolve_email_credentials(session, account, request.app.state.settings)
        account_config = email_account_config_for(account, request.app.state.settings)
        subject = f"Re: {c.contact_email or 'conversation'}"
    else:
        from app.services.marketing.connections import resolve_account_credentials

        credentials = await resolve_account_credentials(session, account, request.app.state.settings)
        account_config = account.config_metadata or {}
        subject = None

    result = await provider.send(
        account_config=account_config,
        recipient_address=recipient_address,
        subject=subject,
        body=payload.body,
        idempotency_key=f"conv:{c.id}:{uuid.uuid4()}",
        metadata={"conversation_id": str(c.id), "replied_by": str(ctx.user.id)},
        credentials=credentials,
    )

    message = Message(
        conversation_id=c.id,
        direction=MessageDirection.SENT.value,
        message_type="TEXT",
        body=payload.body,
        status=MessageStatus.SENT.value if result.ok else MessageStatus.FAILED.value,
        provider_message_id=result.provider_message_id,
        message_metadata={
            "provider": account.provider,
            "sending_account_id": str(account.id),
            "error": result.error,
            "error_code": result.error_code,
        },
    )
    session.add(message)
    c.last_message_at = datetime.now(timezone.utc)
    await session.commit()

    await request.app.state.audit.log(
        session,
        action="conversation.replied",
        actor_user_id=ctx.user.id,
        resource_type="conversation",
        resource_id=str(c.id),
        ip_address=get_client_ip(request),
        metadata={
            "channel": c.channel,
            "delivered": result.ok,
            "error_code": result.error_code if not result.ok else None,
        },
    )
    return ReplyOut(
        data=MessageOut(
            id=str(message.id),
            conversation_id=str(message.conversation_id),
            direction=message.direction,
            message_type=message.message_type,
            body=message.body,
            status=message.status,
            provider_message_id=message.provider_message_id,
            metadata=message.message_metadata or {},
            created_at=message.created_at,
        )
    )


@router.post("/bulk-assignment", response_model=BulkAssignmentOut)
async def bulk_assign_conversations(
    payload: BulkAssignmentIn,
    request: Request,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("inbox.assign")),
):
    """Idempotent bulk assignment: same target = no-op, always history-logged."""
    new_user_id = uuid.UUID(payload.assigned_user_id) if payload.assigned_user_id else None
    new_team_id = uuid.UUID(payload.assigned_team_id) if payload.assigned_team_id else None
    updated, skipped = 0, 0
    for raw_id in payload.ids[:5000]:
        try:
            cid = uuid.UUID(raw_id)
        except ValueError:
            skipped += 1
            continue
        try:
            c = await authz.get_visible_or_404(session, Conversation, cid, ctx)
        except NotFoundError:
            skipped += 1
            continue
        if c.assigned_user_id == new_user_id and c.assigned_team_id == new_team_id:
            skipped += 1
            continue
        history = ConversationAssignmentHistory(
            conversation_id=c.id,
            organization_id=ctx.organization_id,
            previous_user_id=c.assigned_user_id,
            previous_team_id=c.assigned_team_id,
            assigned_user_id=new_user_id,
            assigned_team_id=new_team_id,
            changed_by=ctx.user.id,
            reason=payload.reason or "BULK_ASSIGNMENT",
        )
        session.add(history)
        c.assigned_user_id = new_user_id
        c.assigned_team_id = new_team_id
        updated += 1
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="conversation.bulk_assigned",
        actor_user_id=ctx.user.id,
        resource_type="conversation",
        resource_id=None,
        ip_address=get_client_ip(request),
        metadata={"updated": updated, "skipped": skipped},
    )
    return BulkAssignmentOut(data={"updated": updated, "skipped": skipped})
