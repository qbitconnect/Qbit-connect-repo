"""BaseTrigger + the pluggable trigger registry (§6).

Each trigger type declares:
- TRIGGER_TYPE constant (stored on Workflow.trigger_type)
- EVENT_TYPES — the system event names that can fire it (§12)
- validate_config(config) — publish-time validation
- build_context(event) — refs extracted from the event payload
- describe(config) — human-readable label for UI/API

Triggers are PURE matchers: they never execute anything.
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.automation.core.exceptions import ConfigurationError


class BaseTrigger:
    TRIGGER_TYPE: ClassVar[str] = ""
    EVENT_TYPES: ClassVar[tuple[str, ...]] = ()
    #: canonical entity refs this trigger provides (subset of lead/conversation/
    #: message/campaign/recipient/entity)
    ENTITY_TYPE: ClassVar[str] = "entity"

    def validate_config(self, config: dict[str, Any]) -> None:
        """Override for trigger-specific configuration validation (§21)."""

    def matches(self, event_type: str, payload: dict[str, Any], config: dict[str, Any]) -> bool:
        """Trigger-specific event filter (e.g. LEAD_TAG_ADDED with a tag filter).
        Default: every event of the declared EVENT_TYPES matches."""
        return True

    def describe(self, config: dict[str, Any]) -> str:
        return self.TRIGGER_TYPE

    def build_context(
        self, event_type: str, payload: dict[str, Any], entity_id: Any = None
    ) -> dict[str, Any]:
        """Return the refs dict stored on the execution context (§34)."""
        return {"entity_id": str(entity_id) if entity_id else None}


class TriggerRegistry:
    def __init__(self) -> None:
        self._triggers: dict[str, BaseTrigger] = {}

    def register(self, trigger: BaseTrigger) -> None:
        if not trigger.TRIGGER_TYPE:
            raise ConfigurationError("Trigger must declare TRIGGER_TYPE")
        self._triggers[trigger.TRIGGER_TYPE] = trigger

    def get(self, trigger_type: str | None) -> BaseTrigger | None:
        return self._triggers.get(trigger_type or "")

    def require(self, trigger_type: str | None) -> BaseTrigger:
        trigger = self._triggers.get(trigger_type or "")
        if trigger is None:
            raise ConfigurationError(f"Unknown trigger type: {trigger_type!r}")
        return trigger

    def all(self) -> dict[str, BaseTrigger]:
        return dict(self._triggers)

    def event_index(self) -> dict[str, list[str]]:
        """Map: system event type → [TRIGGER_TYPE, ...] (§12 dispatch)."""
        index: dict[str, list[str]] = {}
        for trigger in self._triggers.values():
            for event_type in trigger.EVENT_TYPES:
                index.setdefault(event_type, []).append(trigger.TRIGGER_TYPE)
        return index
