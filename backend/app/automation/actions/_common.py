"""Shared helpers for action implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.automation.core.exceptions import ConfigurationError, PermanentError

if TYPE_CHECKING:  # pragma: no cover
    from app.automation.core.context import WorkflowContext
    from app.models.messaging import Conversation


async def require_lead(context: "WorkflowContext"):
    """Load the context lead or fail permanently (e.g. deleted/merged away)."""
    lead = await context.load_lead()
    if lead is None:
        raise PermanentError("Lead not available for this workflow execution")
    return lead


async def require_conversation(context: "WorkflowContext") -> "Conversation":
    """Load the context conversation or fail permanently."""
    conversation = await context.load_conversation()
    if conversation is None:
        raise PermanentError("Conversation not available for this workflow execution")
    return conversation


def as_uuid(value):
    import uuid

    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid UUID: {value!r}") from exc
