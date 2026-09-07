"""Trigger implementations (§7–§11, §55).

Every trigger is a pure matcher + context builder. Event types are the
normalized system event names the dispatcher receives from service-layer
hooks (never provider-specific payloads — §10).
"""

from __future__ import annotations

from typing import Any

from app.automation.core.exceptions import ConfigurationError
from app.automation.core.trigger import BaseTrigger


# --------------------------------------------------------------------- leads
class LeadTrigger(BaseTrigger):
    ENTITY_TYPE = "lead"

    def build_context(self, event_type, payload, entity_id=None) -> dict[str, Any]:
        return {"lead_id": str(entity_id) if entity_id else None}


class LEAD_CREATED(LeadTrigger):
    TRIGGER_TYPE = "LEAD_CREATED"
    EVENT_TYPES = ("lead.created",)
    LABEL = "Lead created"


class LEAD_UPDATED(LeadTrigger):
    TRIGGER_TYPE = "LEAD_UPDATED"
    EVENT_TYPES = ("lead.updated",)
    LABEL = "Lead updated"


class LEAD_STATUS_CHANGED(LeadTrigger):
    TRIGGER_TYPE = "LEAD_STATUS_CHANGED"
    EVENT_TYPES = ("lead.status_changed",)
    LABEL = "Lead status changed"

    def validate_config(self, config: dict[str, Any]) -> None:
        _optional_statuses(config)

    def matches(self, event_type, payload, config) -> bool:
        statuses = (config or {}).get("statuses") or []
        if not statuses:
            return True
        return payload.get("to_status") in statuses or payload.get("new_status") in statuses


class LEAD_TAG_ADDED(LeadTrigger):
    TRIGGER_TYPE = "LEAD_TAG_ADDED"
    EVENT_TYPES = ("lead.tag_added",)
    LABEL = "Tag added to lead"

    def validate_config(self, config: dict[str, Any]) -> None:
        _optional_tag(config)

    def matches(self, event_type, payload, config) -> bool:
        tag = (config or {}).get("tag")
        if not tag:
            return True
        return str(payload.get("tag") or "").lower() == str(tag).lower()


class LEAD_TAG_REMOVED(LeadTrigger):
    TRIGGER_TYPE = "LEAD_TAG_REMOVED"
    EVENT_TYPES = ("lead.tag_removed",)
    LABEL = "Tag removed from lead"

    def validate_config(self, config: dict[str, Any]) -> None:
        _optional_tag(config)

    def matches(self, event_type, payload, config) -> bool:
        tag = (config or {}).get("tag")
        if not tag:
            return True
        return str(payload.get("tag") or "").lower() == str(tag).lower()


class LEAD_IMPORTED(LeadTrigger):
    TRIGGER_TYPE = "LEAD_IMPORTED"
    EVENT_TYPES = ("lead.imported",)
    LABEL = "Lead imported"


class LEAD_SCRAPED(LeadTrigger):
    TRIGGER_TYPE = "LEAD_SCRAPED"
    EVENT_TYPES = ("lead.scraped",)
    LABEL = "Lead scraped"


# ------------------------------------------------------------------ campaign
class CampaignTrigger(BaseTrigger):
    ENTITY_TYPE = "campaign_recipient"

    def build_context(self, event_type, payload, entity_id=None) -> dict[str, Any]:
        refs = dict(payload.get("refs") or {})
        return {
            "campaign_id": refs.get("campaign_id"),
            "recipient_id": refs.get("recipient_id"),
            "lead_id": refs.get("lead_id"),
        }


class CAMPAIGN_COMPLETED(CampaignTrigger):
    TRIGGER_TYPE = "CAMPAIGN_COMPLETED"
    EVENT_TYPES = ("campaign.completed",)
    ENTITY_TYPE = "campaign"
    LABEL = "Campaign completed"


class CAMPAIGN_FAILED(CampaignTrigger):
    TRIGGER_TYPE = "CAMPAIGN_FAILED"
    EVENT_TYPES = ("campaign.failed",)
    ENTITY_TYPE = "campaign"
    LABEL = "Campaign failed"


class CAMPAIGN_RECIPIENT_REPLIED(CampaignTrigger):
    TRIGGER_TYPE = "CAMPAIGN_RECIPIENT_REPLIED"
    EVENT_TYPES = ("campaign.recipient.replied",)
    LABEL = "Campaign recipient replied"


class CAMPAIGN_RECIPIENT_FAILED(CampaignTrigger):
    TRIGGER_TYPE = "CAMPAIGN_RECIPIENT_FAILED"
    EVENT_TYPES = ("campaign.recipient.failed",)
    LABEL = "Campaign recipient failed"


# -------------------------------------------------------------- conversation
class ConversationTrigger(BaseTrigger):
    ENTITY_TYPE = "conversation"

    def build_context(self, event_type, payload, entity_id=None) -> dict[str, Any]:
        refs = dict(payload.get("refs") or {})
        return {
            "conversation_id": refs.get("conversation_id") or (str(entity_id) if entity_id else None),
            "lead_id": refs.get("lead_id"),
            "message_id": refs.get("message_id"),
        }


class CONVERSATION_CREATED(ConversationTrigger):
    TRIGGER_TYPE = "CONVERSATION_CREATED"
    EVENT_TYPES = ("conversation.created",)
    LABEL = "Conversation created"


class INBOUND_MESSAGE(ConversationTrigger):
    """Inbound message on a conversation (§9, §56)."""

    TRIGGER_TYPE = "INBOUND_MESSAGE"
    EVENT_TYPES = ("conversation.inbound_message",)
    LABEL = "Inbound message received"


class CONVERSATION_ASSIGNED(ConversationTrigger):
    TRIGGER_TYPE = "CONVERSATION_ASSIGNED"
    EVENT_TYPES = ("conversation.assigned",)
    LABEL = "Conversation assigned"


class CONVERSATION_STATUS_CHANGED(ConversationTrigger):
    TRIGGER_TYPE = "CONVERSATION_STATUS_CHANGED"
    EVENT_TYPES = ("conversation.status_changed",)
    LABEL = "Conversation status changed"

    def validate_config(self, config: dict[str, Any]) -> None:
        status = config.get("status")
        if status is not None and not isinstance(status, str):
            raise ConfigurationError("conversation status filter must be a string")

    def matches(self, event_type, payload, config) -> bool:
        status = (config or {}).get("status")
        if not status:
            return True
        return payload.get("new_status") == status or payload.get("to_status") == status


class CONVERSATION_REOPENED(ConversationTrigger):
    TRIGGER_TYPE = "CONVERSATION_REOPENED"
    EVENT_TYPES = ("conversation.reopened",)
    LABEL = "Conversation reopened"


# ------------------------------------------------------------------- message
class MessageTrigger(BaseTrigger):
    """Normalized message events (§10) — provider-independent."""

    ENTITY_TYPE = "message"

    def build_context(self, event_type, payload, entity_id=None) -> dict[str, Any]:
        refs = dict(payload.get("refs") or {})
        return {
            "message_id": refs.get("message_id") or (str(entity_id) if entity_id else None),
            "conversation_id": refs.get("conversation_id"),
            "lead_id": refs.get("lead_id"),
            "campaign_id": refs.get("campaign_id"),
            "recipient_id": refs.get("recipient_id"),
        }


class MESSAGE_RECEIVED(MessageTrigger):
    TRIGGER_TYPE = "MESSAGE_RECEIVED"
    EVENT_TYPES = ("message.received",)
    LABEL = "Message received"


class MESSAGE_SENT(MessageTrigger):
    TRIGGER_TYPE = "MESSAGE_SENT"
    EVENT_TYPES = ("message.sent",)
    LABEL = "Message sent"


class MESSAGE_DELIVERED(MessageTrigger):
    TRIGGER_TYPE = "MESSAGE_DELIVERED"
    EVENT_TYPES = ("message.delivered",)
    LABEL = "Message delivered"


class MESSAGE_FAILED(MessageTrigger):
    TRIGGER_TYPE = "MESSAGE_FAILED"
    EVENT_TYPES = ("message.failed",)
    LABEL = "Message failed"


# ------------------------------------------------------------------ schedule
class SCHEDULED(BaseTrigger):
    """Time-based trigger (§11): daily at HH:MM, hourly, fixed interval, or a
    one-time run. Timezone-aware (never assumes one country's timezone)."""

    TRIGGER_TYPE = "SCHEDULED"
    EVENT_TYPES = ("schedule.tick",)
    ENTITY_TYPE = "schedule"
    LABEL = "Scheduled"

    def validate_config(self, config: dict[str, Any]) -> None:
        from app.automation.triggers.schedule import validate_schedule_config

        validate_schedule_config(config)


def _optional_statuses(config: dict[str, Any]) -> None:
    statuses = config.get("statuses") or []
    if not isinstance(statuses, list) or not all(isinstance(s, str) for s in statuses):
        raise ConfigurationError("'statuses' must be a list of strings")


def _optional_tag(config: dict[str, Any]) -> None:
    tag = config.get("tag")
    if tag is not None and not isinstance(tag, str):
        raise ConfigurationError("'tag' must be a string")


def build_trigger_registry() -> Any:
    """Registry preloaded with every Phase 9 trigger (§7–§11)."""
    from app.automation.core.trigger import TriggerRegistry

    registry = TriggerRegistry()
    for trigger in (
        LEAD_CREATED(), LEAD_UPDATED(), LEAD_STATUS_CHANGED(),
        LEAD_TAG_ADDED(), LEAD_TAG_REMOVED(), LEAD_IMPORTED(), LEAD_SCRAPED(),
        CAMPAIGN_COMPLETED(), CAMPAIGN_FAILED(),
        CAMPAIGN_RECIPIENT_REPLIED(), CAMPAIGN_RECIPIENT_FAILED(),
        CONVERSATION_CREATED(), INBOUND_MESSAGE(), CONVERSATION_ASSIGNED(),
        CONVERSATION_STATUS_CHANGED(), CONVERSATION_REOPENED(),
        MESSAGE_RECEIVED(), MESSAGE_SENT(), MESSAGE_DELIVERED(), MESSAGE_FAILED(),
        SCHEDULED(),
    ):
        registry.register(trigger)
    return registry
