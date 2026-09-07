"""Declarative definition schemas (§4, §5, §14, §20, §35).

Workflow definitions are pure data — validated by Pydantic models with
`extra="forbid"` so unknown keys (and any attempt to smuggle code) are
rejected at publish time. There is NO expression language: conditions are
field/operator/value triples, variables are allowlisted `{{path}}` slots.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.automation import NodeTypes

#: variable slot grammar: {{lead.first_name}} — dot-path into entity snapshots
VARIABLE_RE = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*)\s*\}\}")

NODE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TriggerNodeConfig(StrictModel):
    type: str = Field(min_length=1, max_length=50)


class ConditionClause(StrictModel):
    field: str = Field(min_length=1, max_length=120)
    operator: str = Field(min_length=1, max_length=20)
    value: Any = None


class ConditionGroup(StrictModel):
    """Declarative boolean group (§20): AND/OR/NOT, safely nestable."""

    all: list["ConditionClause | ConditionGroup"] | None = None
    any: list["ConditionClause | ConditionGroup"] | None = None
    not_: ConditionClause | ConditionGroup | None = Field(default=None, alias="not")
    field: str | None = None
    operator: str | None = None
    value: Any = None

    @model_validator(mode="after")
    def _shape(self) -> "ConditionGroup":
        is_clause = self.field is not None
        is_group = self.all is not None or self.any is not None or self.not_ is not None
        if is_clause == is_group:
            raise ValueError(
                "condition must be either a clause ({field, operator, value}) "
                "or a group ({all|any|not})"
            )
        if is_clause and not self.operator:
            raise ValueError("condition clause requires an operator")
        if self.all is not None and not self.all:
            raise ValueError("'all' must not be empty")
        if self.any is not None and not self.any:
            raise ValueError("'any' must not be empty")
        return self

    def model_dump(self, **kwargs):
        data = super().model_dump(**kwargs)
        if "not_" in data and data["not_"] is not None:
            data["not"] = data.pop("not_")
        return data


class WaitDuration(StrictModel):
    seconds: int | None = Field(default=None, ge=0, le=60)
    minutes: int | None = Field(default=None, ge=0, le=1440)
    hours: int | None = Field(default=None, ge=0, le=720)
    days: int | None = Field(default=None, ge=0, le=90)


class NodeSpec(StrictModel):
    id: str = Field(pattern=NODE_ID_RE.pattern)
    type: Literal["TRIGGER", "CONDITION", "ACTION", "WAIT", "BRANCH", "END"]
    next_node_id: str | None = None
    next_node_id_no: str | None = None  # CONDITION "NO" branch (§31)
    # TRIGGER
    trigger_config: dict[str, Any] | None = None
    # CONDITION / BRANCH
    condition: ConditionGroup | None = None
    # BRANCH only
    branches: list["BranchSpec"] | None = None
    # ACTION
    action: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    # WAIT
    duration: WaitDuration | None = None
    respect_business_hours: bool = False
    business_hours: dict[str, Any] | None = None


class BranchSpec(StrictModel):
    """One IF-branch of a BRANCH node (§31) — first match wins."""

    condition: ConditionGroup
    next_node_id: str


class WorkflowDefinition(StrictModel):
    """The full workflow graph (§4)."""

    nodes: list[NodeSpec] = Field(min_length=2, max_length=500)

    def get_node(self, node_id: str | None) -> NodeSpec | None:
        if node_id is None:
            return None
        return next((n for n in self.nodes if n.id == node_id), None)

    @property
    def trigger_node(self) -> NodeSpec:
        return next(n for n in self.nodes if n.type == NodeTypes.TRIGGER)


class TriggerConfig(StrictModel):
    """Trigger configuration (validated further per trigger type §6)."""

    config: dict[str, Any] = Field(default_factory=dict)


class ScheduleTriggerConfig(StrictModel):
    """SCHEDULED trigger configuration (§11). Timezone-aware; never assumes a
    specific country's timezone."""

    schedule_type: Literal["daily", "hourly", "interval", "once"]
    time: str | None = None  # "HH:MM" for daily
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    run_at: str | None = None  # ISO datetime for once
    timezone: str = "UTC"


BusinessHoursConfig = dict  # {"days": [0..4], "start": "09:00", "end": "18:00", "timezone": "UTC"}


def render_variables(template: str, resolve) -> tuple[str, list[str]]:
    """Safely substitute {{path}} slots (§35) using the given resolver.

    Returns (rendered_text, unresolved_paths). The resolver only ever sees
    allowlisted dotted paths — there is no evaluation of arbitrary content.
    """
    unresolved: list[str] = []

    def _sub(match: re.Match) -> str:
        path = match.group(1)
        value = resolve(path)
        if value is None:
            unresolved.append(path)
            return ""
        return str(value)

    return VARIABLE_RE.sub(_sub, template or ""), sorted(set(unresolved))


ConditionLike = ConditionClause | ConditionGroup
