"""Entity snapshot builders + the condition field catalog (§15–§19).

Snapshots are lightweight dicts of safe, workflow-visible fields loaded fresh
from the database at condition-evaluation time. They contain IDs and business
fields only — no secrets — and are what `{{variables}}` and conditions resolve
against. One snapshot per entity key ("lead", "conversation", "message",
"campaign", "recipient"); condition fields are dotted paths like
`lead.city` / `message.body` / `campaign.status`.
"""

from __future__ import annotations

from typing import Any

#: The publish-time field catalog (§21 "field exists"). Maps field path → type tag.
FIELD_CATALOG: dict[str, str] = {
    # lead (§15)
    "lead.status": "string",
    "lead.quality_score": "number",
    "lead.source": "string",
    "lead.category": "string",
    "lead.industry": "string",
    "lead.city": "string",
    "lead.state": "string",
    "lead.country": "string",
    "lead.email": "string",
    "lead.phone": "string",
    "lead.website": "string",
    "lead.business_name": "string",
    "lead.contact_name": "string",
    "lead.has_email": "boolean",
    "lead.has_phone": "boolean",
    "lead.tags": "list",
    # tag conditions (§16) — special-cased in the engine
    "lead.has_tag": "special",
    "lead.does_not_have_tag": "special",
    # message (§17)
    "message.body": "string",
    "message.subject": "string",
    "message.direction": "string",
    "message.message_type": "string",
    "message.status": "string",
    "message.channel": "string",
    # conversation (§18)
    "conversation.status": "string",
    "conversation.priority": "string",
    "conversation.channel": "string",
    "conversation.assigned_user": "string",
    "conversation.assigned_team": "string",
    "conversation.unread_count": "number",
    # campaign (§19)
    "campaign.status": "string",
    "campaign.channel": "string",
    "recipient.status": "string",
    "recipient.replied": "boolean",
    "recipient.delivered": "boolean",
    "recipient.failed": "boolean",
}


def lead_snapshot(lead: Any) -> dict[str, Any]:
    """`lead.*` fields (§15) + tag mirror for has_tag/does_not_have_tag (§16)."""
    if lead is None:
        return {}
    return {
        "id": str(lead.id),
        "status": lead.status,
        "quality_score": lead.quality_score,
        "source": lead.source,
        "category": lead.category,
        "industry": lead.industry,
        "city": lead.city,
        "state": lead.state,
        "country": lead.country,
        "email": lead.email,
        "phone": lead.phone,
        "website": lead.website,
        "business_name": lead.business_name,
        "contact_name": lead.contact_name,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "has_email": bool(lead.email),
        "has_phone": bool(lead.phone),
        "tags": list(lead.tags or []),
    }


def conversation_snapshot(conversation: Any) -> dict[str, Any]:
    """`conversation.*` fields (§18)."""
    if conversation is None:
        return {}
    return {
        "id": str(conversation.id),
        "status": conversation.status,
        "priority": conversation.priority,
        "channel": conversation.channel,
        "assigned_user": str(conversation.assigned_user_id) if conversation.assigned_user_id else "",
        "assigned_team": str(conversation.assigned_team_id) if conversation.assigned_team_id else "",
        "unread_count": int(conversation.unread_count or 0),
        "lead_id": str(conversation.lead_id) if conversation.lead_id else None,
    }


def message_snapshot(message: Any, channel: str | None = None) -> dict[str, Any]:
    """`message.*` fields (§17). Channel comes from the parent conversation."""
    if message is None:
        return {}
    return {
        "id": str(message.id),
        "body": message.body or "",
        "subject": message.subject or "",
        "direction": message.direction,
        "message_type": message.message_type,
        "status": message.status,
        "channel": channel or "",
    }


def campaign_snapshot(campaign: Any) -> dict[str, Any]:
    """`campaign.*` fields (§19)."""
    if campaign is None:
        return {}
    return {
        "id": str(campaign.id),
        "status": campaign.status,
        "channel": campaign.channel,
        "name": campaign.name,
    }


def recipient_snapshot(recipient: Any) -> dict[str, Any]:
    """`recipient.*` fields (§19): status / reply / delivery state."""
    if recipient is None:
        return {}
    return {
        "id": str(recipient.id),
        "status": recipient.status,
        "replied": bool(recipient.replied_at),
        "delivered": bool(recipient.delivered_at),
        "failed": bool(recipient.failed_at),
    }
