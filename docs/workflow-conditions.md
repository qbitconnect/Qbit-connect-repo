# Workflow Conditions (Phase 9 §14–§21)

Conditions are **declarative** field/operator/value clauses grouped with
AND/OR/NOT. There is no expression language — nothing is ever evaluated as
code (§63). Unknown fields/operators and type-unsafe comparisons are rejected
at publish time and raise `CONFIGURATION` errors at runtime (honest failure,
never silent pass).

## Clause shape

```json
{"field": "lead.quality_score", "operator": "greater_than", "value": 70}
```

## Operators (§14)

`equals` · `not_equals` · `contains` · `not_contains` · `starts_with` ·
`ends_with` · `is_empty` · `is_not_empty` · `greater_than` · `less_than` ·
`greater_or_equal` · `less_or_equal` · `between` · `in` · `not_in`

Semantics notes:
- Numeric operators coerce numeric strings; a **NULL field value never
  satisfies a numeric comparison** (returns False honestly).
- Booleans normalise: `true`/`"true"` are interchangeable.
- `contains` is case-insensitive substring; `in`/`not_in` take a list.
- `between` requires `[min, max]`.

## Group shape (§20)

```json
{"all": [clause, clause, …]}          // AND
{"any": [clause, clause, …]}          // OR
{"not": clause-or-group}              // NOT
```

Groups nest safely up to depth 5 (`MAX_GROUP_DEPTH`). A node condition is
either a clause or a group — never both.

## Field catalog (§15–§19)

| Entity | Fields |
|---|---|
| `lead.*` (§15) | `status, quality_score, source, category, industry, city, state, country, email, phone, website, business_name, contact_name, first_name, last_name, has_email, has_phone, tags` |
| tag special (§16) | `lead.has_tag` (value = tag name), `lead.does_not_have_tag` |
| `message.*` (§17) | `body, subject, direction, message_type, status, channel` |
| `conversation.*` (§18) | `status, priority, channel, assigned_user, assigned_team, unread_count` |
| `campaign.*` (§19) | `status, channel` |
| `recipient.*` (§19) | `status, replied, delivered, failed` |

Entity snapshots are loaded fresh from the DB at each node visit and contain
business fields + IDs only — never secrets (§73).

## Examples

§75 TEST 8 — quality score AND email presence:

```json
{"all": [
  {"field": "lead.quality_score", "operator": "greater_than", "value": 70},
  {"field": "lead.has_email", "operator": "equals", "value": true}
]}
```

Inbox keyword routing (§9/§56):

```json
{"all": [
  {"field": "message.channel", "operator": "equals", "value": "WHATSAPP"},
  {"field": "message.body", "operator": "contains", "value": "price"}
]}
```

Tag conditions (§16):

```json
{"field": "lead.has_tag", "operator": "equals", "value": "Hot"}
{"field": "lead.does_not_have_tag", "operator": "equals", "value": "Contacted"}
```

## Validation (§21)

At publish the engine validates every condition: field exists in the catalog,
operator supported, value type correct (numeric ops need numeric values,
`between` needs a pair, `in` needs a list), group shape valid, nesting within
depth. Invalid workflows cannot be activated.
