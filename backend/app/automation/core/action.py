"""BaseAction + the action registry (§22).

Actions are modular, declarative-configured units. Each action class:
- ACTION_KEY constant ("add_tag", "send_email", ...)
- validate_config(config) — publish-time validation (DB-aware validation
  happens in WorkflowService, which passes a session)
- execute(context, config) -> dict — structured output (§36); raises
  ActionSkipped for legitimate skips (§27) or AutomationError subclasses
  for failures (§37)
- describe(config) — human-readable label

Actions MUST be idempotent where practical (§39) and MUST NOT log or store
secrets (§36, §76).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from app.automation.core.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: no cover
    from app.automation.core.context import WorkflowContext


class BaseAction:
    ACTION_KEY: ClassVar[str] = ""
    #: short human label for the builder palette (§43)
    LABEL: ClassVar[str] = ""

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        """Publish-time validation. `session` may be None for pure-JSON checks."""

    async def execute(self, context: "WorkflowContext", config: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def describe(self, config: dict[str, Any]) -> str:
        return self.LABEL or self.ACTION_KEY


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, BaseAction] = {}

    def register(self, action: BaseAction) -> None:
        if not action.ACTION_KEY:
            raise ConfigurationError("Action must declare ACTION_KEY")
        self._actions[action.ACTION_KEY] = action

    def get(self, key: str | None) -> BaseAction | None:
        return self._actions.get(key or "")

    def require(self, key: str | None) -> BaseAction:
        action = self._actions.get(key or "")
        if action is None:
            raise ConfigurationError(f"Unknown action: {key!r}")
        return action

    def all(self) -> dict[str, BaseAction]:
        return dict(self._actions)
