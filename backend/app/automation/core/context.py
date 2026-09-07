"""Workflow execution context (§34, §35, §73).

The context carries *references* (IDs), never copies of large objects.
Entity snapshots are loaded lazily from the DB, fresh at each condition
evaluation / variable render, and cached per node execution. Only safe,
allowlisted snapshot fields are exposed — personal data stays in the source
tables, snapshots hold business fields and IDs (§73).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.conditions import catalog
from app.automation.core.exceptions import ConfigurationError
from app.automation.core.schemas import render_variables
from app.models.automation import WorkflowExecution
from app.models.marketing import Campaign, CampaignRecipient
from app.models.messaging import Conversation, Message
from app.models.scrape import Lead


class WorkflowContext:
    """Runtime context for one execution (one node visit)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        execution: WorkflowExecution,
        settings: Any,
        now: datetime | None = None,
    ) -> None:
        self.session = session
        self.execution = execution
        self.settings = settings
        self.now = now or datetime.now(timezone.utc)
        ctx = execution.context or {}
        self.trigger_event_id: str = execution.trigger_event_id
        self.trigger_type: str = str(ctx.get("trigger_type", ""))
        self.refs: dict[str, Any] = dict(ctx.get("refs") or {})
        self.variables: dict[str, Any] = dict(ctx.get("variables") or {})
        self.depth: int = int(ctx.get("depth") or 0)
        self.correlation_id: str | None = execution.correlation_id
        self.causation_id: str | None = execution.causation_id
        self._snapshots: dict[str, dict | None] = {}
        self._loaded = False

    # ------------------------------------------------------------------ refs
    @property
    def lead_id(self) -> uuid.UUID | None:
        return _as_uuid(self.refs.get("lead_id") or self.refs.get("entity_id"))

    @property
    def conversation_id(self) -> uuid.UUID | None:
        return _as_uuid(self.refs.get("conversation_id"))

    @property
    def message_id(self) -> uuid.UUID | None:
        return _as_uuid(self.refs.get("message_id"))

    @property
    def campaign_id(self) -> uuid.UUID | None:
        return _as_uuid(self.refs.get("campaign_id"))

    @property
    def recipient_id(self) -> uuid.UUID | None:
        return _as_uuid(self.refs.get("recipient_id"))

    @property
    def entity_id(self) -> uuid.UUID | None:
        return self.execution.entity_id

    # -------------------------------------------------------------- snapshots
    async def preload(self) -> None:
        """Load all entity snapshots for the current refs (async) so the sync
        condition engine and variable renderer can read them (§34). Called at
        each node visit — snapshots reflect the latest committed state."""
        self._snapshots = {}
        # conversation (also provides channel for message snapshot)
        conversation_row = None
        if self.conversation_id:
            conversation_row = await self.session.get(Conversation, self.conversation_id)
            self._snapshots["conversation"] = (
                catalog.conversation_snapshot(conversation_row)
                if conversation_row is not None else None
            )
        # lead (follows soft merges)
        if self.lead_id:
            lead_row = await self.session.get(Lead, self.lead_id)
            if lead_row is not None and lead_row.merged_into_id is not None:
                merged = await self.session.get(Lead, lead_row.merged_into_id)
                if merged is not None:
                    lead_row = merged
            self._snapshots["lead"] = (
                catalog.lead_snapshot(lead_row) if lead_row is not None else None
            )
        elif conversation_row is not None and conversation_row.lead_id:
            lead_row = await self.session.get(Lead, conversation_row.lead_id)
            self._snapshots["lead"] = (
                catalog.lead_snapshot(lead_row) if lead_row is not None else None
            )
        # message
        if self.message_id:
            message_row = await self.session.get(Message, self.message_id)
            if message_row is not None:
                channel = conversation_row.channel if conversation_row else None
                self._snapshots["message"] = catalog.message_snapshot(message_row, channel)
        # campaign + recipient
        if self.campaign_id:
            campaign_row = await self.session.get(Campaign, self.campaign_id)
            self._snapshots["campaign"] = (
                catalog.campaign_snapshot(campaign_row) if campaign_row is not None else None
            )
        if self.recipient_id:
            recipient_row = await self.session.get(CampaignRecipient, self.recipient_id)
            self._snapshots["recipient"] = (
                catalog.recipient_snapshot(recipient_row) if recipient_row is not None else None
            )

    def _resolver_map(self) -> dict[str, Callable[[], dict | None]]:
        """Snapshot resolvers per entity key — read the preloaded cache."""

        def resolve(key: str) -> dict | None:
            return self._snapshots.get(key)

        return {
            "lead": lambda: resolve("lead"),
            "conversation": lambda: resolve("conversation"),
            "message": lambda: resolve("message"),
            "campaign": lambda: resolve("campaign"),
            "recipient": lambda: resolve("recipient"),
        }

    def condition_engine(self):
        """ConditionEngine bound to this context's snapshots (fresh load)."""
        from app.automation.core.condition import ConditionEngine

        return ConditionEngine(self._resolver_map())

    def _load_lead(self) -> dict | None:
        lead_id = self.lead_id
        if lead_id is None and self.conversation_id:
            return None
        if lead_id is None:
            return None
        lead = self.session.get(Lead, lead_id)
        return catalog.lead_snapshot(lead) if lead is not None else None

    def _load_conversation(self) -> dict | None:
        conv_id = self.conversation_id
        if conv_id is None:
            return None
        conversation = self.session.get(Conversation, conv_id)
        return catalog.conversation_snapshot(conversation) if conversation is not None else None

    def _load_message(self) -> dict | None:
        msg_id = self.message_id
        if msg_id is None:
            return None
        message = self.session.get(Message, msg_id)
        if message is None:
            return None
        channel = None
        if message.conversation_id:
            conversation = self.session.get(Conversation, message.conversation_id)
            channel = conversation.channel if conversation else None
        return catalog.message_snapshot(message, channel)

    def _load_campaign(self) -> dict | None:
        campaign_id = self.campaign_id
        if campaign_id is None:
            return None
        campaign = self.session.get(Campaign, campaign_id)
        return catalog.campaign_snapshot(campaign) if campaign is not None else None

    def _load_recipient(self) -> dict | None:
        recipient_id = self.recipient_id
        if recipient_id is None:
            return None
        recipient = self.session.get(CampaignRecipient, recipient_id)
        return catalog.recipient_snapshot(recipient) if recipient is not None else None

    # ------------------------------------------------------------- ORM access
    async def load_lead(self) -> Lead | None:
        lead_id = self.lead_id
        if lead_id is None:
            return None
        lead = await self.session.get(Lead, lead_id)
        if lead is not None and lead.merged_into_id is not None:
            # follow soft merges so actions always hit the surviving lead
            merged = await self.session.get(Lead, lead.merged_into_id)
            if merged is not None:
                return merged
        return lead

    async def load_conversation(self) -> Conversation | None:
        conv_id = self.conversation_id
        if conv_id:
            conversation = await self.session.get(Conversation, conv_id)
            if conversation is not None:
                return conversation
        # fall back: the lead's most recent conversation (§56 inbox linkage)
        lead = await self.load_lead()
        if lead is not None:
            from sqlalchemy import desc, select

            row = await self.session.execute(
                select(Conversation)
                .where(Conversation.lead_id == lead.id)
                .order_by(desc(Conversation.last_message_at), Conversation.id)
                .limit(1)
            )
            return row.scalars().first()
        return None

    async def load_message(self) -> Message | None:
        msg_id = self.message_id
        return await self.session.get(Message, msg_id) if msg_id else None

    async def load_campaign(self) -> Campaign | None:
        campaign_id = self.campaign_id
        return await self.session.get(Campaign, campaign_id) if campaign_id else None

    async def load_recipient(self) -> CampaignRecipient | None:
        recipient_id = self.recipient_id
        return await self.session.get(CampaignRecipient, recipient_id) if recipient_id else None

    # -------------------------------------------------------------- variables
    def resolve_path(self, path: str) -> Any:
        """Resolve an allowlisted dotted path against entity snapshots (§35).

        Unknown paths resolve to None (rendered as empty string) — no
        exceptions, no expression evaluation, no shell/meta characters.
        """
        entity_key, _, attr = path.partition(".")
        if not attr:
            raise ConfigurationError(f"Invalid variable path: {path!r}")
        snapshot = self._resolver_map().get(entity_key, lambda: None)()
        if snapshot is None:
            return None
        return snapshot.get(attr)

    def render(self, template: str) -> tuple[str, list[str]]:
        """Render `{{path}}` slots in a template string (§35)."""
        return render_variables(template, self.resolve_path)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
