"""Starter workflow templates (Phase 9 §52).

These are DEFINITION TEMPLATES only — creating a workflow from a template
always produces a DRAFT that the user must configure and publish themselves.
Nothing here is ever auto-activated.
"""

from __future__ import annotations

from typing import Any


def _n(node_id: str, node_type: str, **extra) -> dict:
    return {"id": node_id, "type": node_type, **extra}


TEMPLATE_CATALOG: dict[str, dict[str, Any]] = {
    "new_lead_qualification": {
        "name": "New Lead Qualification",
        "description": (
            "When a lead is created: tag it by quality and notify assignment. "
            "Configure the quality threshold and tags before publishing."
        ),
        "trigger_type": "LEAD_CREATED",
    },
    "whatsapp_reply_assignment": {
        "name": "New WhatsApp Reply Assignment",
        "description": (
            "When an inbound WhatsApp message arrives: detect keywords and "
            "assign the conversation. Configure the keyword list and assignee."
        ),
        "trigger_type": "INBOUND_MESSAGE",
    },
    "email_reply_assignment": {
        "name": "New Email Reply Assignment",
        "description": (
            "When an inbound email message arrives: detect keywords in the "
            "body and assign the conversation. Configure keywords and assignee."
        ),
        "trigger_type": "INBOUND_MESSAGE",
    },
    "high_quality_lead_tagging": {
        "name": "High Quality Lead Tagging",
        "description": (
            "When a lead is created and its quality score crosses your "
            "threshold, tag it as high quality. Configure the threshold."
        ),
        "trigger_type": "LEAD_CREATED",
    },
    "unresponsive_lead_followup": {
        "name": "Unresponsive Lead Follow-up",
        "description": (
            "Wait after a new lead is created; if the conversation never "
            "received a reply, add a follow-up note. Configure the wait."
        ),
        "trigger_type": "LEAD_CREATED",
    },
}


def template_definition(template_id: str) -> dict:
    """Return a DRAFT-definition graph for a template (never auto-activated)."""
    if template_id == "new_lead_qualification":
        return {
            "nodes": [
                _n("trigger", "TRIGGER", trigger_config={"type": "LEAD_CREATED"},
                   next_node_id="cond_quality"),
                _n("cond_quality", "CONDITION",
                   condition={"field": "lead.quality_score", "operator": "greater_than", "value": 70},
                   next_node_id="tag_hot", next_node_id_no="tag_review"),
                _n("tag_hot", "ACTION", action="add_tag", config={"tag": "High Quality"},
                   next_node_id="end"),
                _n("tag_review", "ACTION", action="add_tag", config={"tag": "Review"},
                   next_node_id="end"),
                _n("end", "END"),
            ],
        }
    if template_id == "whatsapp_reply_assignment":
        return {
            "nodes": [
                _n("trigger", "TRIGGER", trigger_config={"type": "INBOUND_MESSAGE"},
                   next_node_id="cond_keyword"),
                _n("cond_keyword", "CONDITION",
                   condition={"all": [
                       {"field": "message.channel", "operator": "equals", "value": "WHATSAPP"},
                       {"field": "message.body", "operator": "contains", "value": "price"},
                   ]},
                   next_node_id="assign", next_node_id_no="end"),
                _n("assign", "ACTION", action="assign_user", config={"user_id": "REPLACE_WITH_USER_ID"},
                   next_node_id="priority"),
                _n("priority", "ACTION", action="change_priority", config={"priority": "HIGH"},
                   next_node_id="end"),
                _n("end", "END"),
            ],
        }
    if template_id == "email_reply_assignment":
        return {
            "nodes": [
                _n("trigger", "TRIGGER", trigger_config={"type": "INBOUND_MESSAGE"},
                   next_node_id="cond_keyword"),
                _n("cond_keyword", "CONDITION",
                   condition={"all": [
                       {"field": "message.channel", "operator": "equals", "value": "EMAIL"},
                       {"field": "message.subject", "operator": "contains", "value": "quotation"},
                   ]},
                   next_node_id="assign", next_node_id_no="end"),
                _n("assign", "ACTION", action="assign_user", config={"user_id": "REPLACE_WITH_USER_ID"},
                   next_node_id="end"),
                _n("end", "END"),
            ],
        }
    if template_id == "high_quality_lead_tagging":
        return {
            "nodes": [
                _n("trigger", "TRIGGER", trigger_config={"type": "LEAD_CREATED"},
                   next_node_id="cond_score"),
                _n("cond_score", "CONDITION",
                   condition={"all": [
                       {"field": "lead.quality_score", "operator": "greater_or_equal", "value": 80},
                       {"field": "lead.has_email", "operator": "equals", "value": True},
                   ]},
                   next_node_id="tag", next_node_id_no="end"),
                _n("tag", "ACTION", action="add_tag", config={"tag": "Hot"},
                   next_node_id="end"),
                _n("end", "END"),
            ],
        }
    if template_id == "unresponsive_lead_followup":
        return {
            "nodes": [
                _n("trigger", "TRIGGER", trigger_config={"type": "LEAD_CREATED"},
                   next_node_id="wait"),
                _n("wait", "WAIT", duration={"days": 3}, next_node_id="cond_replied"),
                _n("cond_replied", "CONDITION",
                   condition={"field": "lead.status", "operator": "not_equals", "value": "REPLIED"},
                   next_node_id="note", next_node_id_no="end"),
                _n("note", "ACTION", action="add_note",
                   config={"content": "Follow-up: no reply after 3 days (automation)."},
                   next_node_id="end"),
                _n("end", "END"),
            ],
        }
    raise KeyError(template_id)
