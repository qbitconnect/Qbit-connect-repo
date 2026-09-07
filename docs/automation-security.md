# Automation Security (Phase 9 §61–§65, §73, §76, §83)

## The core rule (§63)

**Workflow definitions are data, never code.** The engine contains no
`eval`, `exec`, `subprocess` or any dynamic code execution; no arbitrary HTTP
request nodes exist (§64 leaves external webhooks to a future, allowlisted
integration system). Definitions are parsed by Pydantic models with
`extra="forbid"` — unknown keys are rejected outright, so nothing can be
smuggled through.

## Attack surface & controls (§65)

| Threat | Control |
|---|---|
| Workflow injection (code in definitions/config) | strict Pydantic schemas, node/field/operator/action catalogs, node-id regex, size caps; unknown anything → publish rejected |
| Template/variable injection (§35) | `{{dotted.path}}` grammar only, resolved against allowlisted entity snapshots; unknown paths render empty; no expressions, no filters, no shell |
| XSS via rendered variables | server-rendered Jinja2 autoescaping in the UI; notes/messages store plain text |
| SQL injection | ORM-only parameterized queries everywhere in the engine |
| IDOR | detail routes return 404 for missing/unpermitted resources (no existence leak) — platform convention |
| RBAC bypass (§61) | `require_permission` on every route; publish gated by `automation.publish`; cancel/retry by `automation.execute`; UI mirrors but never guards |
| Unauthorized execution | executions are only created by the dispatcher (service hooks + worker); the API can only cancel/retry; no user-triggerable raw execution endpoint |
| Unauthorized publishing (TEST 9) | viewer/operator roles get 403 on publish — verified in tests |
| Infinite loops (§40) | acyclic graph at publish + causation depth cap + per-entity window limit + max-steps/execution + node-count cap |
| Duplicate executions (§13) | `UNIQUE(workflow_id, trigger_event_id)` + unique intake `event_id` |
| Duplicate sends (§39) | deterministic `client_message_id` through the reply idempotency; ADD_TAG/ASSIGN inherently idempotent |
| Invalid graphs / node refs (§65) | publish validation: reachability, acyclicity, valid targets, both branches required on conditions |
| Malicious variable content | rendered text is escaped in UI, stored as plain text, never executed |
| Oversized workflows (§65) | `QBIT_AUTOMATION_MAX_NODES` + Pydantic bounds on definition structure |
| Excessive execution depth / resource exhaustion (§65, §42) | depth cap, window limit, step budget per cycle, batch size cap, wait cap |

## Secrets & privacy (§73, §76)

- Execution step snapshots hold **configuration and safe outputs only** — no
  credentials, no raw provider responses.
- Intake event payloads pass through the platform `redact()` mask.
- Context snapshots contain business fields + IDs; sensitive personal data
  stays in the source tables (references, not copies).
- Structured logs never include API keys, tokens, passwords, cookies or
  message secrets.

## RBAC matrix (§61)

| Permission | SUPER_ADMIN | ADMIN | MANAGER | OPERATOR | VIEWER |
|---|---|---|---|---|---|
| automation.view | ✓ | ✓ | ✓ | ✓ | ✓ |
| automation.create / edit / publish / pause / resume | ✓ | ✓ | ✓ | — | — |
| automation.execute | ✓ | ✓ | ✓ | ✓ | — |
| automation.delete | ✓ | ✓ | — | — | — |
| automation.view_executions | ✓ | ✓ | ✓ | ✓ | ✓ |

Every workflow mutation (created/edited/published/paused/resumed/archived/
duplicated/deleted) and execution lifecycle (started/completed/failed/
cancelled) is audit-logged with actor attribution (§62) — secrets never.

## Compliance boundary preserved

Automation reuses the marketing/inbox guard stack; it cannot send to
suppressed, unsubscribed, opted-out or window-closed recipients (verified by
tests). There is no automatic consent generation, no stealth automation, no
provider-restriction bypass — Phase 9 §83 non-goals are honoured.
