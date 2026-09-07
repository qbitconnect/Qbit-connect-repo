"""Inbox workspace queries (Phase 8 §9–§15, §36, §46–§48, §50, §62).

Server-side everything: filtering, search (indexed LIKE), pagination
(offset for the conversation list, cursor for message history) and unread
counters. The browser never loads the whole mailbox (§11, §47, §48).

Visibility (§50) is enforced IN SQL, not in the UI:
- users with inbox.manage see everything (scope ALL)
- otherwise the QBIT_INBOX_VISIBILITY setting decides: ALL, or ASSIGNED_ONLY
  (conversations assigned to the current user; unassigned stay visible to
  the team). TEAM mode is accepted as ALL until a team model exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.services.inbox.normalizer import utcnow
from app.models.messaging import (
    Conversation,
    ConversationEvent,
    ConversationNote,
    ConversationPriority,
    ConversationStatus,
    Message,
)
from app.models.scrape import Lead
from app.models.user import User

VALID_STATUSES = [s.value for s in ConversationStatus]
VALID_PRIORITIES = [p.value for p in ConversationPriority]
VALID_CHANNELS = ("WHATSAPP", "EMAIL")

#: conversations assigned to nobody — visible to every in-scope user
UNASSIGNED_SCOPING = "unassigned"


def _has_manage(perms: set[str] | None) -> bool:
    return bool(perms) and "inbox.manage" in perms


class InboxWorkspace:
    """Read/query side of the inbox. Mutations live in ConversationEngine."""

    # ------------------------------------------------------------- visibility
    def _visibility_clause(self, *, user_id: uuid.UUID, permissions: set[str] | None,
                           settings):
        if _has_manage(permissions):
            return None  # unrestricted
        mode = (getattr(settings, "QBIT_INBOX_VISIBILITY", "ALL") or "ALL").upper()
        if mode == "ASSIGNED_ONLY":
            # assigned to me OR unassigned (never another agent's private queue)
            return or_(
                Conversation.assigned_user_id == user_id,
                Conversation.assigned_user_id.is_(None),
            )
        return None

    # ----------------------------------------------------------------- list
    async def list_conversations(
        self, session: AsyncSession, *,
        user_id: uuid.UUID, permissions: set[str] | None, settings,
        channel: str | None = None,
        status: str | None = None,
        assignment: str | None = None,       # mine | unassigned | user:<id>
        priority: str | None = None,
        unread_only: bool = False,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        lead_tag: str | None = None,
        lead_status: str | None = None,
        lead_source: str | None = None,
        search: str | None = None,
        page: int = 1, page_size: int = 25,
    ) -> tuple[list[dict], int]:
        query = select(Conversation)
        filters = []

        visibility = self._visibility_clause(
            user_id=user_id, permissions=permissions, settings=settings,
        )
        if visibility is not None:
            filters.append(visibility)

        if channel:
            channel = channel.upper()
            if channel not in VALID_CHANNELS:
                raise ValidationError(f"Unknown channel '{channel}'")
            filters.append(Conversation.channel == channel)
        if status:
            status = status.upper()
            if status not in VALID_STATUSES:
                raise ValidationError(f"Unknown status '{status}'")
            filters.append(Conversation.status == status)
        if priority:
            priority = priority.upper()
            if priority not in VALID_PRIORITIES:
                raise ValidationError(f"Unknown priority '{priority}'")
            filters.append(Conversation.priority == priority)
        if unread_only:
            filters.append(Conversation.unread_count > 0)
        if assignment == "mine":
            filters.append(Conversation.assigned_user_id == user_id)
        elif assignment == UNASSIGNED_SCOPING:
            filters.append(Conversation.assigned_user_id.is_(None))
        elif assignment and assignment.startswith("user:"):
            try:
                filters.append(Conversation.assigned_user_id == uuid.UUID(assignment[5:]))
            except ValueError as exc:
                raise ValidationError("Invalid user id in assignment filter") from exc
        if date_from is not None:
            filters.append(Conversation.last_message_at >= date_from)
        if date_to is not None:
            filters.append(Conversation.last_message_at <= date_to)

        lead_filters = []
        if lead_tag:
            lead_filters.append(Lead.tags.contains([lead_tag]))
        if lead_status:
            lead_filters.append(Lead.status == lead_status.upper())
        if lead_source:
            lead_filters.append(Lead.source == lead_source)
        if search:
            term = f"%{search.strip().lower()}%"
            lead_filters.append(or_(
                func.lower(func.coalesce(Lead.business_name, "")).like(term),
                func.lower(func.coalesce(Lead.contact_name, "")).like(term),
                func.lower(func.coalesce(Lead.email, "")).like(term),
                func.lower(func.coalesce(Lead.phone, "")).like(term),
                func.lower(func.coalesce(Conversation.contact_phone, "")).like(term),
                func.lower(func.coalesce(Conversation.contact_email, "")).like(term),
                func.lower(func.coalesce(Conversation.subject, "")).like(term),
                func.lower(func.coalesce(Conversation.external_contact_id, "")).like(term),
            ))

        if lead_filters:
            query = query.outerjoin(Lead, Lead.id == Conversation.lead_id)
        if filters:
            query = query.where(*filters)
        if lead_filters:
            query = query.where(*lead_filters)

        total = (await session.execute(
            select(func.count()).select_from(query.subquery())
        )).scalar_one()

        rows = (await session.execute(
            query
            .order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.created_at.desc(),
            )
            .offset(max(0, (page - 1) * page_size))
            .limit(min(max(page_size, 1), 200))
        )).scalars().all()

        items = []
        for conversation in rows:
            items.append(await self._conversation_row(session, conversation))
        return items, total

    async def _conversation_row(self, session: AsyncSession, conversation: Conversation) -> dict:
        data = conversation.to_public_dict()
        lead = None
        if conversation.lead_id:
            lead = await session.get(Lead, conversation.lead_id)
        data["lead"] = {
            "id": str(lead.id),
            "business_name": lead.business_name,
            "contact_name": lead.contact_name,
            "status": lead.status,
            "quality_score": lead.quality_score,
            "tags": lead.tags or [],
        } if lead else None
        preview = await session.execute(
            select(Message.direction, Message.body, Message.message_type, Message.created_at)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        row = preview.first()
        data["last_message"] = {
            "direction": row.direction,
            "preview": (row.body or ("" if row.message_type == "TEXT" else f"[{row.message_type.lower()}]"))[:120],
            "created_at": row.created_at.isoformat() if row.created_at else None,
        } if row else None
        return data

    # --------------------------------------------------------------- detail
    async def get_conversation(
        self, session: AsyncSession, conversation_id: uuid.UUID, *,
        user_id: uuid.UUID, permissions: set[str] | None, settings,
    ) -> dict:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found")
        visibility = self._visibility_clause(
            user_id=user_id, permissions=permissions, settings=settings,
        )
        if visibility is not None:
            # §50: backend-enforced visibility — invisible rows 404, never 403
            check = await session.execute(
                select(Conversation.id).where(
                    Conversation.id == conversation_id, visibility
                ).limit(1)
            )
            if check.first() is None:
                raise NotFoundError("Conversation not found")
        data = conversation.to_public_dict()
        account_name = None
        if conversation.sending_account_id:
            from app.models.marketing import SendingAccount

            account = await session.get(SendingAccount, conversation.sending_account_id)
            if account is not None:
                account_name = account.display_identifier or account.identifier
                data["account"] = account.to_public_dict()
        data["sending_account_name"] = account_name
        lead_payload = None
        if conversation.lead_id:
            lead = await session.get(Lead, conversation.lead_id)
            if lead is not None:
                lead_payload = lead.to_public_dict()
        data["lead"] = lead_payload
        assignee = None
        if conversation.assigned_user_id:
            user = await session.get(User, conversation.assigned_user_id)
            assignee = {"id": str(user.id), "full_name": user.full_name,
                        "email": user.email} if user else None
        data["assignee"] = assignee
        return data

    # ------------------------------------------------------------- messages
    async def list_messages(
        self, session: AsyncSession, conversation_id: uuid.UUID, *,
        before: datetime | None = None, limit: int = 50,
    ) -> tuple[list[dict], datetime | None]:
        """Cursor pagination — newest page first, then scroll upward (§48)."""
        query = select(Message).where(Message.conversation_id == conversation_id)
        if before is not None:
            query = query.where(Message.created_at < before)
        rows = (await session.execute(
            query.order_by(Message.created_at.desc()).limit(min(max(limit, 1), 200))
        )).scalars().all()
        next_before = rows[-1].created_at if len(rows) == min(max(limit, 1), 200) else None
        items = [m.to_public_dict() for m in reversed(rows)]  # chronological
        return items, next_before

    async def count_messages(self, session: AsyncSession, conversation_id: uuid.UUID) -> int:
        return (await session.execute(
            select(func.count()).select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )).scalar_one()

    # ----------------------------------------------------------- read state
    async def mark_read(self, session: AsyncSession, conversation: Conversation) -> Conversation:
        conversation.unread_count = 0
        conversation.updated_at = utcnow()
        await session.commit()
        return conversation

    async def mark_unread(self, session: AsyncSession, conversation: Conversation) -> Conversation:
        conversation.unread_count = max(1, conversation.unread_count or 0)
        conversation.updated_at = utcnow()
        await session.commit()
        return conversation

    async def bulk_read_state(
        self, session: AsyncSession, *, conversation_ids: list[uuid.UUID],
        unread: bool, user_id: uuid.UUID, permissions: set[str] | None, settings,
    ) -> int:
        """Bulk Mark Read/Unread (§46). Visibility still applies per row."""
        changed = 0
        for cid in conversation_ids[:500]:
            conversation = await session.get(Conversation, cid)
            if conversation is None:
                continue
            visibility = self._visibility_clause(
                user_id=user_id, permissions=permissions, settings=settings,
            )
            if visibility is not None:
                check = await session.execute(
                    select(Conversation.id).where(
                        Conversation.id == cid, visibility
                    ).limit(1)
                )
                if check.first() is None:
                    continue
            if unread:
                conversation.unread_count = max(1, conversation.unread_count or 0)
            else:
                conversation.unread_count = 0
            changed += 1
        await session.commit()
        return changed

    # -------------------------------------------------------------- counters
    async def unread_counters(
        self, session: AsyncSession, *,
        user_id: uuid.UUID, permissions: set[str] | None, settings,
    ) -> dict:
        visibility = self._visibility_clause(
            user_id=user_id, permissions=permissions, settings=settings,
        )
        base = select(
            Conversation.channel,
            func.coalesce(func.sum(Conversation.unread_count), 0).label("unread"),
            func.count().label("conversations"),
        ).group_by(Conversation.channel)
        mine = select(
            func.coalesce(func.sum(Conversation.unread_count), 0)
        ).where(Conversation.assigned_user_id == user_id)
        if visibility is not None:
            base = base.where(visibility)
            mine = mine.where(visibility)
        rows = (await session.execute(base)).all()
        assigned_to_me = (await session.execute(mine)).scalar_one()
        counters = {"total": 0, "whatsapp": 0, "email": 0, "assigned_to_me": 0,
                    "conversations": 0}
        for channel, unread, count in rows:
            counters["conversations"] += count
            if channel == "WHATSAPP":
                counters["whatsapp"] = int(unread or 0)
            elif channel == "EMAIL":
                counters["email"] = int(unread or 0)
            counters["total"] += int(unread or 0)
        counters["assigned_to_me"] = int(assigned_to_me or 0)
        return counters

    # ---------------------------------------------------------------- search
    async def search_messages(
        self, session: AsyncSession, *, query_text: str,
        user_id: uuid.UUID, permissions: set[str] | None, settings,
        channel: str | None = None, limit: int = 50,
    ) -> list[dict]:
        """Server-side message search (§11, §47) — body/subject/provider id."""
        term = f"%{query_text.strip().lower()}%"
        if not query_text.strip():
            return []
        query = (
            select(Message, Conversation)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(or_(
                func.lower(func.coalesce(Message.body, "")).like(term),
                func.lower(func.coalesce(Message.subject, "")).like(term),
                func.lower(func.coalesce(Message.provider_message_id, "")).like(term),
            ))
        )
        visibility = self._visibility_clause(
            user_id=user_id, permissions=permissions, settings=settings,
        )
        if visibility is not None:
            query = query.where(visibility)
        if channel:
            channel = channel.upper()
            if channel not in VALID_CHANNELS:
                raise ValidationError(f"Unknown channel '{channel}'")
            query = query.where(Conversation.channel == channel)
        rows = (await session.execute(
            query.order_by(Message.created_at.desc()).limit(min(limit, 200))
        )).all()
        results = []
        for message, conversation in rows:
            results.append({
                "message": message.to_public_dict(),
                "conversation": {
                    "id": str(conversation.id),
                    "channel": conversation.channel,
                    "contact_phone": conversation.contact_phone,
                    "contact_email": conversation.contact_email,
                    "subject": conversation.subject,
                },
            })
        return results

    # -------------------------------------------------------------- activity
    async def list_activity(
        self, session: AsyncSession, conversation_id: uuid.UUID, *,
        limit: int = 200,
    ) -> list[dict]:
        """Merged, chronological activity timeline: events + notes (§29, §34)."""
        events = (await session.execute(
            select(ConversationEvent)
            .where(ConversationEvent.conversation_id == conversation_id)
            .order_by(ConversationEvent.created_at.desc())
            .limit(min(limit, 500))
        )).scalars().all()
        notes = (await session.execute(
            select(ConversationNote)
            .where(ConversationNote.conversation_id == conversation_id)
            .order_by(ConversationNote.created_at.desc())
            .limit(min(limit, 500))
        )).scalars().all()

        entries: list[dict] = []
        user_ids: set[uuid.UUID] = set()
        for event in events:
            if event.actor_user_id:
                user_ids.add(event.actor_user_id)
            entries.append({"kind": "event", "at": event.created_at,
                            "event": event.to_public_dict()})
        for note in notes:
            if note.user_id:
                user_ids.add(note.user_id)
            entries.append({"kind": "note", "at": note.created_at,
                            "note": note.to_public_dict()})
        entries.sort(key=lambda e: e["at"], reverse=True)

        users = {}
        if user_ids:
            for user in (await session.execute(
                select(User).where(User.id.in_(list(user_ids)))
            )).scalars().all():
                users[user.id] = {"id": str(user.id), "full_name": user.full_name,
                                  "email": user.email}
        for entry in entries:
            actor = None
            if entry["kind"] == "event":
                actor = entry["event"].get("actor_user_id")
            else:
                actor = entry["note"].get("user_id")
            entry["user"] = users.get(uuid.UUID(actor)) if actor else None
        return entries[:limit]

    async def list_notes(self, session: AsyncSession, conversation_id: uuid.UUID) -> list[dict]:
        rows = (await session.execute(
            select(ConversationNote)
            .where(ConversationNote.conversation_id == conversation_id)
            .order_by(ConversationNote.created_at.desc())
        )).scalars().all()
        return [note.to_public_dict() for note in rows]

    async def get_note(
        self, session: AsyncSession, conversation_id: uuid.UUID, note_id: uuid.UUID,
    ) -> ConversationNote:
        note = await session.get(ConversationNote, note_id)
        if note is None or note.conversation_id != conversation_id:
            raise NotFoundError("Note not found")
        return note

