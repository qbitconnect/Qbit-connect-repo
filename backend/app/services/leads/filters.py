"""Advanced filter engine (Phase 4 §11, §13).

Filters arrive as JSON groups:

    {"and": [
        {"field": "state", "op": "eq", "value": "Gujarat"},
        {"field": "has_email", "op": "eq", "value": true},
        {"or": [
            {"field": "city", "op": "eq", "value": "Ahmedabad"},
            {"field": "city", "op": "eq", "value": "Surat"}
        ]}
    ]}

Everything is validated server-side against a whitelist — unknown fields,
unknown operators and bad value types are rejected (never passed to SQL).
Sorting uses a whitelist of columns; user input can never reach ORDER BY raw.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, not_, or_, select
from sqlalchemy.sql.sqltypes import NullType

from app.core.errors import ValidationError
from app.models.lead import LeadTag, LeadTagAssignment
from app.models.scrape import Lead

MAX_GROUP_DEPTH = 3
MAX_CONDITIONS = 100

#: field -> (column or callable, kind)
TEXT_FIELDS = (
    "business_name", "contact_name", "first_name", "last_name", "email", "phone",
    "website", "address", "city", "state", "postal_code", "country", "category",
    "industry", "source", "source_type", "source_id",
)

FILTERABLE: dict[str, tuple[str, str]] = {f: (f, "text") for f in TEXT_FIELDS}
FILTERABLE.update(
    {
        "status": ("status", "text"),
        "quality_score": ("quality_score", "int"),
        "rating": ("rating", "float"),
        "review_count": ("review_count", "int"),
        "seen_count": ("seen_count", "int"),
        "created_at": ("created_at", "datetime"),
        "updated_at": ("updated_at", "datetime"),
        "scraped_at": ("scraped_at", "datetime"),
        "source_actor_id": ("source_actor_id", "text"),
        "source_job_id": ("source_job_id", "uuid"),
        "import_batch_id": ("import_batch_id", "uuid"),
        # virtual fields
        "has_phone": ("phone", "has"),
        "has_email": ("email", "has"),
        "has_website": ("website", "has"),
        "has_address": ("address", "has"),
        "tag": ("__tag__", "tag"),
    }
)

#: whitelisted sortable columns (§13)
SORTABLE: dict[str, Any] = {
    "business_name": Lead.business_name,
    "created_at": Lead.created_at,
    "updated_at": Lead.updated_at,
    "quality_score": Lead.quality_score,
    "status": Lead.status,
    "city": Lead.city,
    "state": Lead.state,
    "country": Lead.country,
    "source": Lead.source,
    "email": Lead.email,
    "phone": Lead.phone,
    "scraped_at": Lead.scraped_at,
    "seen_count": Lead.seen_count,
}

OPERATORS = {
    "eq", "neq", "contains", "starts_with", "ends_with", "empty", "not_empty",
    "gt", "gte", "lt", "lte", "between",
}

_NO_VALUE_OPS = {"empty", "not_empty"}
_RANGE_OPS = {"gt", "gte", "lt", "lte", "between"}


def _column_for(field: str):
    attr = FILTERABLE[field][0]
    if attr == "__tag__":
        return None
    return getattr(Lead, attr)


def _coerce_single(kind: str, value: Any) -> Any:
    try:
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "datetime":
            if isinstance(value, (datetime, date)):
                return value
            text = str(value)
            if len(text) == 10:  # date-only
                return datetime.fromisoformat(text + "T00:00:00+00:00")
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        if kind == "uuid":
            import uuid as uuid_mod

            return str(uuid_mod.UUID(str(value)))
        if kind == "has":
            return bool(value)
        return str(value)
    except (ValueError, TypeError, IndexError) as exc:
        raise ValidationError(f"Invalid filter value: {value!r}") from exc


def _coerce(kind: str, op: str, value: Any, field: str) -> Any:
    if op in _NO_VALUE_OPS:
        return None
    if value is None:
        raise ValidationError(f"Filter '{field}' operator '{op}' requires a value")
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValidationError(f"Filter '{field}' between requires [low, high]")
        return [_coerce_single(kind, v) for v in value]
    return _coerce_single(kind, value)


def _has_condition(column, wanted: bool) -> ColumnElement:
    non_empty = (column.isnot(None)) & (column != "")
    return non_empty if wanted else not_(non_empty)


def _leaf_condition(field: str, op: str, value: Any) -> ColumnElement:
    kind = FILTERABLE[field][1]
    if kind == "tag":
        if op not in ("eq", "neq"):
            raise ValidationError(f"Filter 'tag' supports operators: eq, neq")
        tag_name = str(value)
        subq = (
            select(LeadTagAssignment.lead_id)
            .join(LeadTag, LeadTag.id == LeadTagAssignment.tag_id)
            .where(LeadTag.name == tag_name)
        )
        return Lead.id.in_(subq) if op == "eq" else not_(Lead.id.in_(subq))

    if kind == "has":
        if op not in ("eq",):
            raise ValidationError(f"Filter '{field}' supports operator: eq (true/false)")
        return _has_condition(_column_for(field), bool(value))

    column = _column_for(field)
    coerced = _coerce(kind, op, value, field)
    if op == "eq":
        return column == coerced
    if op == "neq":
        return column != coerced
    if op == "contains":
        escaped = str(coerced).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return column.ilike(f"%{escaped}%", escape="\\")
    if op == "starts_with":
        escaped = str(coerced).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return column.ilike(f"{escaped}%", escape="\\")
    if op == "ends_with":
        escaped = str(coerced).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return column.ilike(f"%{escaped}", escape="\\")
    if op == "empty":
        return (column.is_(None)) | (column == "")
    if op == "not_empty":
        return (column.isnot(None)) & (column != "")
    if op == "gt":
        return column > coerced
    if op == "gte":
        return column >= coerced
    if op == "lt":
        return column < coerced
    if op == "lte":
        return column <= coerced
    if op == "between":
        return column.between(coerced[0], coerced[1])
    raise ValidationError(f"Unsupported filter operator: {op}")


def build_filter_condition(spec: Any, depth: int = 0) -> ColumnElement:
    """Recursive, validated group → SQLAlchemy condition.

    A single leaf condition ({"field","op","value"}) is also accepted."""
    if depth > MAX_GROUP_DEPTH:
        raise ValidationError("Filter groups nested too deeply (max 3)")
    if isinstance(spec, dict) and "field" in spec:
        unknown = set(spec.keys()) - {"field", "op", "value"}
        if unknown:
            raise ValidationError(f"Unknown filter keys: {sorted(unknown)}")
        field = spec.get("field")
        op = (spec.get("op") or "eq").strip().lower()
        if field not in FILTERABLE:
            raise ValidationError(f"Unknown filter field: {field!r}")
        if op not in OPERATORS:
            raise ValidationError(f"Unknown filter operator: {op!r}")
        return _leaf_condition(field, op, spec.get("value"))
    if isinstance(spec, list):
        spec = {"and": spec}
    if not isinstance(spec, dict) or not spec:
        raise ValidationError("Filter must be a non-empty object or list")
    if len(spec) > 1:
        raise ValidationError("Filter group must have exactly one of: and / or")
    mode, body = next(iter(spec.items()))
    mode = mode.strip().lower()
    if mode not in ("and", "or"):
        raise ValidationError(f"Unknown filter group '{mode}' (use 'and' / 'or')")
    if not isinstance(body, list) or not body:
        raise ValidationError(f"Filter group '{mode}' must be a non-empty list")
    if len(body) > MAX_CONDITIONS:
        raise ValidationError(f"Too many filter conditions (max {MAX_CONDITIONS})")

    conditions = []
    for item in body:
        if isinstance(item, dict) and ("and" in item or "or" in item):
            conditions.append(build_filter_condition(item, depth + 1))
            continue
        if not isinstance(item, dict):
            raise ValidationError("Each filter condition must be an object")
        unknown = set(item.keys()) - {"field", "op", "value"}
        if unknown:
            raise ValidationError(f"Unknown filter keys: {sorted(unknown)}")
        field = item.get("field")
        op = (item.get("op") or "eq").strip().lower()
        if field not in FILTERABLE:
            raise ValidationError(f"Unknown filter field: {field!r}")
        if op not in OPERATORS:
            raise ValidationError(f"Unknown filter operator: {op!r}")
        conditions.append(_leaf_condition(field, op, item.get("value")))

    return and_(*conditions) if mode == "and" else or_(*conditions)


def build_order_by(sort: str | None) -> list[Any]:
    """Parse 'field,-field2' against the whitelist. SQL-injection safe."""
    if not sort or not sort.strip():
        return [Lead.created_at.desc()]
    clauses: list[Any] = []
    for token in sort.split(",")[:4]:
        token = token.strip()
        if not token:
            continue
        descending = token.startswith("-")
        field = token[1:] if descending else token
        column = SORTABLE.get(field.lstrip("+"))
        if column is None:
            raise ValidationError(f"Cannot sort by '{field}' (not whitelisted)")
        clauses.append(column.desc() if descending else column.asc())
    if not clauses:
        raise ValidationError("Empty sort expression")
    return clauses
