"""Conversation actions (§25) + assignment actions (§24).

CHANGE_CONVERSATION_STATUS / CHANGE_PRIORITY / ASSIGN_CONVERSATION /
ADD_INTERNAL_NOTE reuse ConversationEngine (existing activity events, RBAC-
safe internals). ASSIGN_USER validates the target user exists; ASSIGN_TEAM
is honest about the current data model — the conversations.assigned_team_id
column is a Phase 8 RESERVED field with no team directory, so team assignment
is disabled unless QBIT_AUTOMATION_ENABLE_TEAM_ASSIGN is switched on (and even
then it only records the raw id, which the rest of the system does not yet
scope by). UNASSIGN clears the assignment.
"""

from __future__ import annotations

from typing import Any

from app.automation.actions._common import as_uuid, require_conversation
from app.automation.core.action import BaseAction
from app.automation.core.exceptions import ActionSkipped, ConfigurationError
from app.models.messaging import ConversationStatus, ConversationPriority


class CHANGE_CONVERSATION_STATUS(BaseAction):
    ACTION_KEY = "change_conversation_status"
    LABEL = "Change conversation status"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        status = config.get("status")
        valid = {s.value for s in ConversationStatus}
        if status not in valid:
            raise ConfigurationError(
                f"Unknown conversation status {status!r} (valid: {sorted(valid)})"
            )

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        conversation = await require_conversation(context)
        from app.services.inbox.engine import ConversationEngine

        updated = await ConversationEngine().change_status(
            context.session, conversation, str(config["status"])
        )
        return {"status": "success", "result": {
            "conversation_id": str(conversation.id), "new_status": updated.status}}


class CHANGE_PRIORITY(BaseAction):
    ACTION_KEY = "change_priority"
    LABEL = "Change conversation priority"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        priority = config.get("priority")
        valid = {p.value for p in ConversationPriority}
        if priority not in valid:
            raise ConfigurationError(
                f"Unknown priority {priority!r} (valid: {sorted(valid)})"
            )

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        conversation = await require_conversation(context)
        from app.services.inbox.engine import ConversationEngine

        updated = await ConversationEngine().change_priority(
            context.session, conversation, str(config["priority"])
        )
        return {"status": "success", "result": {
            "conversation_id": str(conversation.id), "new_priority": updated.priority}}


class ASSIGN_CONVERSATION(BaseAction):
    ACTION_KEY = "assign_conversation"
    LABEL = "Assign conversation to user"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        if not config.get("user_id"):
            raise ConfigurationError("assign_conversation requires user_id")

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        conversation = await require_conversation(context)
        from app.models.user import User
        from app.services.inbox.engine import ConversationEngine

        user_id = as_uuid(config.get("user_id"))
        user = await context.session.get(User, user_id) if user_id else None
        if user is None:
            # target must exist (§24); if it vanished, skip honestly
            raise ActionSkipped("ASSIGN_TARGET_USER_NOT_FOUND")
        updated = await ConversationEngine().assign_user(
            context.session, conversation, user_id
        )
        return {"status": "success", "result": {
            "conversation_id": str(conversation.id),
            "assigned_user_id": str(updated.assigned_user_id) if updated.assigned_user_id else None}}


class ASSIGN_USER(ASSIGN_CONVERSATION):
    """Alias per §24 — assignment actions target conversations (the only
    assignable entity in the current data model)."""

    ACTION_KEY = "assign_user"
    LABEL = "Assign to user"


class ASSIGN_TEAM(BaseAction):
    """Team assignment — honest implementation of a reserved capability (§24).

    There is no Team model yet (Phase 8 reserved `conversations.assigned_team_id`).
    This action stays disabled by default; when enabled it records the raw team
    id on the conversation. It is never presented as a fully supported feature.
    """

    ACTION_KEY = "assign_team"
    LABEL = "Assign to team (reserved)"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        if not getattr(settings, "QBIT_AUTOMATION_ENABLE_TEAM_ASSIGN", False):
            raise ConfigurationError(
                "assign_team is disabled: no team directory exists yet "
                "(QBIT_AUTOMATION_ENABLE_TEAM_ASSIGN=false)"
            )
        if not config.get("team_id"):
            raise ConfigurationError("assign_team requires team_id")

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        conversation = await require_conversation(context)
        team_id = as_uuid(config.get("team_id"))
        conversation.assigned_team_id = team_id
        conversation.updated_at = conversation.updated_at  # no-op; column onupdate covers
        from app.models.messaging import ConversationEvent

        context.session.add(ConversationEvent(
            conversation_id=conversation.id,
            event_type="ASSIGNED",
            previous_value={"assigned_team_id": None},
            new_value={"assigned_team_id": str(team_id), "via": "automation"},
        ))
        await context.session.commit()
        return {"status": "success", "result": {
            "conversation_id": str(conversation.id), "assigned_team_id": str(team_id)}}


class UNASSIGN(BaseAction):
    ACTION_KEY = "unassign_conversation"
    LABEL = "Unassign conversation"

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        conversation = await require_conversation(context)
        if conversation.assigned_user_id is None:
            # idempotent no-op (§39)
            return {"status": "success", "result": {"unassigned": False}}
        from app.services.inbox.engine import ConversationEngine

        await ConversationEngine().assign_user(context.session, conversation, None)
        return {"status": "success", "result": {"unassigned": True}}


class ADD_INTERNAL_NOTE(BaseAction):
    ACTION_KEY = "add_internal_note"
    LABEL = "Add internal conversation note"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        content = config.get("content")
        if not content or not isinstance(content, str) or len(content) > 5000:
            raise ConfigurationError("add_internal_note requires content (max 5000 chars)")

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        conversation = await require_conversation(context)
        rendered, unresolved = context.render(str(config["content"]))
        from app.services.inbox.engine import ConversationEngine

        await ConversationEngine().add_note(
            context.session, conversation, rendered, user_id=None
        )
        return {"status": "success", "result": {
            "conversation_id": str(conversation.id), "note_added": True,
            "unresolved_variables": unresolved}}
