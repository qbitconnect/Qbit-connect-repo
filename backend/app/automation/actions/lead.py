"""Lead actions (§23): UPDATE_LEAD, CHANGE_STATUS, ADD_TAG, REMOVE_TAG,
ADD_NOTE.

All actions reuse the existing lead services (TagService / LeadWorkspaceService)
so idempotency, normalization, quality-score recompute and activity logging
behaviour is identical to manual edits. Automation-caused changes carry
`user_id=None` and are auditable through workflow execution steps.
"""

from __future__ import annotations

from typing import Any

from app.automation.actions._common import require_lead
from app.automation.core.action import BaseAction
from app.automation.core.exceptions import ConfigurationError, PermanentError
from app.models.lead import LeadStatus
from app.models.scrape import Lead

#: fields automation may edit (identity fields like email/phone/business_name
#: are intentionally NOT automation-editable — provenance stays honest)
UPDATABLE_FIELDS = {
    "contact_name", "first_name", "last_name", "address", "city", "state",
    "country", "category", "industry", "website",
}


class UPDATE_LEAD(BaseAction):
    ACTION_KEY = "update_lead"
    LABEL = "Update lead fields"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        fields = config.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ConfigurationError("update_lead requires a non-empty 'fields' object")
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ConfigurationError(
                f"Fields not editable by workflows: {sorted(unknown)}"
            )

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        lead = await require_lead(context)
        fields = {k: v for k, v in (config.get("fields") or {}).items() if k in UPDATABLE_FIELDS}
        if not fields:
            raise ConfigurationError("update_lead requires updatable fields")
        from app.services.leads.service import LeadWorkspaceService

        service = LeadWorkspaceService()
        updated = await service.apply_update(
            context.session, lead, fields, user_id=None, commit=False
        )
        return {"status": "success", "result": {"lead_id": str(updated.id),
                                                "updated_fields": sorted(fields)}}


class CHANGE_STATUS(BaseAction):
    ACTION_KEY = "change_status"
    LABEL = "Change lead status"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        status = config.get("status")
        valid = {s.value for s in LeadStatus}
        if status not in valid:
            raise ConfigurationError(
                f"Unknown lead status {status!r} (valid: {sorted(valid)})"
            )

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        lead = await require_lead(context)
        from app.services.leads.service import LeadWorkspaceService

        service = LeadWorkspaceService()
        updated = await service.set_status(
            context.session, lead, str(config["status"]), user_id=None, commit=False
        )
        return {"status": "success", "result": {"lead_id": str(updated.id),
                                                "new_status": updated.status}}


class ADD_TAG(BaseAction):
    ACTION_KEY = "add_tag"
    LABEL = "Add tag"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        tag = config.get("tag")
        if not tag or not isinstance(tag, str) or len(tag) > 100:
            raise ConfigurationError("add_tag requires a tag name (max 100 chars)")

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        lead = await require_lead(context)
        from app.services.leads.tags import TagService

        tag, created = await TagService().assign(
            context.session, lead.id, str(config["tag"]).strip(), user_id=None, commit=False
        )
        # Idempotent (§39): assigning twice yields created=False the 2nd time.
        return {"status": "success", "result": {
            "tag": tag.name, "created": created, "lead_id": str(lead.id)}}


class REMOVE_TAG(BaseAction):
    ACTION_KEY = "remove_tag"
    LABEL = "Remove tag"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        tag = config.get("tag")
        if not tag or not isinstance(tag, str) or len(tag) > 100:
            raise ConfigurationError("remove_tag requires a tag name (max 100 chars)")

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        lead = await require_lead(context)
        from app.services.leads.tags import TagService
        from sqlalchemy import select

        from app.models.lead import LeadTag

        service = TagService()
        tag_row = (await context.session.execute(
            select(LeadTag).where(LeadTag.name == str(config["tag"]).strip())
        )).scalars().first()
        if tag_row is None:
            # nothing to remove — idempotent no-op, not an error (§39)
            return {"status": "success", "result": {"removed": False}}
        removed = await service.unassign(
            context.session, lead.id, tag_row.id, commit=False
        )
        if not removed:
            return {"status": "success", "result": {"removed": False}}
        return {"status": "success", "result": {"removed": True}}


class ADD_NOTE(BaseAction):
    ACTION_KEY = "add_note"
    LABEL = "Add note to lead"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        content = config.get("content")
        if not content or not isinstance(content, str) or len(content) > 5000:
            raise ConfigurationError("add_note requires content (max 5000 chars)")

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        lead = await require_lead(context)
        rendered, unresolved = context.render(str(config["content"]))
        from app.services.leads.service import LeadWorkspaceService

        await LeadWorkspaceService().add_note(
            context.session, lead.id, rendered, user_id=None, commit=False
        )
        return {"status": "success", "result": {
            "lead_id": str(lead.id), "note_added": True,
            "unresolved_variables": unresolved}}


def _lead_or_none(lead: Lead | None) -> Lead:
    if lead is None:
        raise PermanentError("Lead not found for workflow context")
    return lead
