"""ConditionEngine — declarative condition evaluation (§14–§21).

Conditions are field/operator/value clauses grouped with AND/OR/NOT. Fields
resolve against entity snapshots loaded from the database (fresh at each
evaluation). There is NO expression evaluation — unknown fields, unknown
operators and type-unsafe comparisons raise ConfigurationError so the
execution fails honestly instead of silently passing.
"""

from __future__ import annotations

from typing import Any, Callable

from app.automation.conditions.catalog import FIELD_CATALOG
from app.automation.core.exceptions import ConfigurationError
from app.models.automation import NodeTypes

#: Supported operators (§14)


def _norm_bool(v: Any) -> Any:
    """Normalise booleans to stable strings so `has_email = true` works whether
    the configured value is a JSON boolean or the string "true"."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower()
    return v


def _coerce_pair(a: Any, b: Any) -> tuple[Any, Any]:
    """Numeric-aware comparison pair: when both sides look numeric compare as
    floats, otherwise compare raw (string) values."""
    a, b = _norm_bool(a), _norm_bool(b)
    try:
        return float(a), float(b)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return a, b


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Expected a numeric value, got: {value!r}") from exc


def _num_cmp(a: Any, b: Any, op) -> bool:
    """Numeric comparison with honest NULL semantics: a missing (None) field
    value never satisfies a numeric comparison (returns False) — only a
    non-numeric, non-null value is a configuration error."""
    if a is None:
        return False
    return op(_num(a), _num(b))


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "equals": lambda a, b: _coerce_pair(a, b)[0] == _coerce_pair(a, b)[1],
    "not_equals": lambda a, b: _coerce_pair(a, b)[0] != _coerce_pair(a, b)[1],
    "contains": lambda a, b: str(b).lower() in str(a or "").lower(),
    "not_contains": lambda a, b: str(b).lower() not in str(a or "").lower(),
    "starts_with": lambda a, b: str(a or "").lower().startswith(str(b).lower()),
    "ends_with": lambda a, b: str(a or "").lower().endswith(str(b).lower()),
    "is_empty": lambda a, _b: a is None or (isinstance(a, (str, list, dict)) and len(a) == 0),
    "is_not_empty": lambda a, _b: not (a is None or (isinstance(a, (str, list, dict)) and len(a) == 0)),
    "greater_than": lambda a, b: _num_cmp(a, b, lambda x, y: x > y),
    "less_than": lambda a, b: _num_cmp(a, b, lambda x, y: x < y),
    "greater_or_equal": lambda a, b: _num_cmp(a, b, lambda x, y: x >= y),
    "less_or_equal": lambda a, b: _num_cmp(a, b, lambda x, y: x <= y),
    "between": lambda a, b: (
        a is not None and _num(b[0]) <= _num(a) <= _num(b[1])
    )
    if isinstance(b, (list, tuple)) and len(b) == 2
    else (_raise_config("'between' requires a [min, max] value")),
    "in": lambda a, b: a in (b or []),
    "not_in": lambda a, b: a not in (b or []),
}

OPERATORS_REQUIRING_VALUE = {
    "equals", "not_equals", "contains", "not_contains", "starts_with",
    "ends_with", "greater_than", "less_than", "greater_or_equal",
    "less_or_equal", "between", "in", "not_in",
}

MAX_GROUP_DEPTH = 5


def _raise_config(message: str) -> bool:
    raise ConfigurationError(message)


def validate_clause(clause: dict) -> None:
    """Publish-time validation of one clause (§21)."""
    field = clause.get("field")
    operator = clause.get("operator")
    if field not in FIELD_CATALOG:
        raise ConfigurationError(f"Unknown condition field: {field!r}")
    if operator not in OPERATORS:
        raise ConfigurationError(f"Unsupported operator: {operator!r}")
    value = clause.get("value")
    if operator in OPERATORS_REQUIRING_VALUE and value is None:
        raise ConfigurationError(f"Operator {operator!r} requires a value")
    if operator in ("greater_than", "less_than", "greater_or_equal", "less_or_equal"):
        _num(value)
    if operator == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ConfigurationError("'between' requires a [min, max] value")
        _num(value[0])
        _num(value[1])
    if operator in ("in", "not_in") and not isinstance(value, (list, tuple)):
        raise ConfigurationError(f"Operator {operator!r} requires a list value")


def validate_group(group: dict, depth: int = 0) -> None:
    """Recursively validate a condition group (§20, §21)."""
    if depth > MAX_GROUP_DEPTH:
        raise ConfigurationError("Condition group nesting too deep")
    if group.get("field") is not None:
        validate_clause(group)
        return
    for key in ("all", "any"):
        items = group.get(key)
        if items is not None:
            if not isinstance(items, list) or not items:
                raise ConfigurationError(f"'{key}' must be a non-empty list")
            for item in items:
                validate_group(item, depth + 1)
    not_group = group.get("not")
    if not_group is not None:
        validate_group(not_group, depth + 1)
    if group.get("all") is None and group.get("any") is None and not_group is None:
        raise ConfigurationError("Condition group requires 'all', 'any' or 'not'")


class ConditionEngine:
    """Evaluates condition groups against loaded entity snapshots."""

    def __init__(self, resolvers: dict[str, Callable[[], Any]] | None = None) -> None:
        # resolvers map entity keys ("lead", "conversation", ...) to snapshot dicts
        self._resolvers = resolvers or {}

    def bind(self, resolvers: dict[str, Callable[[], Any]]) -> "ConditionEngine":
        self._resolvers = resolvers
        return self

    # ------------------------------------------------------------------ fields
    def resolve_field(self, field: str) -> Any:
        """Resolve 'lead.city' / 'message.body' / 'lead.has_tag' style paths.

        Special resolvers (has_tag/does_not_have_tag) are handled at clause
        level in evaluate(); everything else is a snapshot dot-path.
        """
        entity_key, _, attr = field.partition(".")
        if not attr:
            raise ConfigurationError(f"Invalid condition field: {field!r}")
        entity = self._entity_snapshot(entity_key)
        if entity is None:
            raise ConfigurationError(
                f"Field {field!r} is not available in this workflow's context"
            )
        if attr not in entity:
            raise ConfigurationError(f"Unknown condition field: {field!r}")
        return entity[attr]

    def _entity_snapshot(self, entity_key: str) -> dict | None:
        resolver = self._resolvers.get(entity_key)
        if resolver is None:
            return None
        snapshot = resolver()
        return snapshot if isinstance(snapshot, dict) else None

    # ---------------------------------------------------------------- evaluate
    def evaluate(self, group: dict | None) -> bool:
        """Evaluate a condition group (§14, §20). None/empty → True."""
        if not group:
            return True
        return bool(self._eval(group, depth=0))

    def _eval(self, group: dict, depth: int) -> bool:
        if depth > MAX_GROUP_DEPTH:
            raise ConfigurationError("Condition group nesting too deep")
        if group.get("field") is not None:
            return self._eval_clause(group)
        if group.get("all") is not None:
            return all(self._eval(item, depth + 1) for item in group["all"])
        if group.get("any") is not None:
            return any(self._eval(item, depth + 1) for item in group["any"])
        if group.get("not") is not None:
            return not self._eval(group["not"], depth + 1)
        raise ConfigurationError("Invalid condition group")

    def _eval_clause(self, clause: dict) -> bool:
        field = clause.get("field") or ""
        operator = clause.get("operator") or ""
        value = clause.get("value")

        # special tag conditions (§16)
        if field in ("lead.has_tag", "has_tag"):
            return self._eval_tags(value, present=True)
        if field in ("lead.does_not_have_tag", "does_not_have_tag"):
            return self._eval_tags(value, present=False)

        if operator not in OPERATORS:
            raise ConfigurationError(f"Unsupported operator: {operator!r}")
        actual = self.resolve_field(field)
        return bool(OPERATORS[operator](actual, value))

    def _eval_tags(self, tag_name: Any, *, present: bool) -> bool:
        if not tag_name or not isinstance(tag_name, str):
            raise ConfigurationError("Tag conditions require a tag name string")
        lead = self._entity_snapshot("lead")
        if lead is None:
            raise ConfigurationError("Tag conditions require a lead context")
        tags = [str(t).lower() for t in (lead.get("tags") or [])]
        has = str(tag_name).lower() in tags
        return has if present else not has


def validate_node_condition(node_type: str, condition: dict | None) -> None:
    if node_type not in (NodeTypes.CONDITION, NodeTypes.BRANCH):
        return
    if node_type == NodeTypes.CONDITION and condition is None:
        raise ConfigurationError("CONDITION node requires a condition")
    if condition is not None:
        validate_group(condition)
