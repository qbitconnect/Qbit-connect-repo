"""Action registry — every Phase 9 action (§23–§27, §54).

The registry is a process-wide singleton so runtime-registered actions (and
tests) share the same instance the validation layer sees.
"""

from __future__ import annotations

from app.automation.core.action import ActionRegistry

_REGISTRY: ActionRegistry | None = None


def build_action_registry() -> ActionRegistry:
    """Return the shared action registry (creates + loads it on first use)."""
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY

    from app.automation.actions.communication import (
        SEND_EMAIL,
        SEND_WHATSAPP,
        START_CAMPAIGN,
    )
    from app.automation.actions.conversation import (
        ADD_INTERNAL_NOTE,
        ASSIGN_CONVERSATION,
        ASSIGN_TEAM,
        ASSIGN_USER,
        CHANGE_CONVERSATION_STATUS,
        CHANGE_PRIORITY,
        UNASSIGN,
    )
    from app.automation.actions.lead import (
        ADD_NOTE,
        ADD_TAG,
        CHANGE_STATUS,
        REMOVE_TAG,
        UPDATE_LEAD,
    )

    registry = ActionRegistry()
    for action in (
        # lead (§23)
        UPDATE_LEAD(), CHANGE_STATUS(), ADD_TAG(), REMOVE_TAG(), ADD_NOTE(),
        # assignment (§24)
        ASSIGN_USER(), ASSIGN_CONVERSATION(), ASSIGN_TEAM(), UNASSIGN(),
        # conversation (§25)
        CHANGE_CONVERSATION_STATUS(), CHANGE_PRIORITY(), ADD_INTERNAL_NOTE(),
        # communication (§26, §54)
        SEND_WHATSAPP(), SEND_EMAIL(), START_CAMPAIGN(),
    ):
        registry.register(action)
    _REGISTRY = registry
    return registry
