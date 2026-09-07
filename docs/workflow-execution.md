# Workflow Execution Model (Phase 9 §28–§42, §49–§51, §57–§59, §71, §76–§77)

## States

```
Execution:  QUEUED → RUNNING → COMPLETED
                        ├──▶ WAITING (next_execution_at) → QUEUED → …
                        ├──▶ QUEUED (retry backoff)
                        ├──▶ FAILED (classified error)
                        └──▶ CANCELLED (operator)
Step:       PENDING → RUNNING → COMPLETED | SKIPPED | FAILED
```

## Queue & claiming (§58, §59)

`workflow_executions` IS the queue (same pattern as `campaign_queue` /
`inbox_outbox`):

1. **Resume due waits** — `WAITING AND next_execution_at <= now → QUEUED`
   (guarded UPDATE; DB is the source of truth, so delays survive restarts —
   TEST 6).
2. **Claim batch** — select due QUEUED ids, then one guarded UPDATE
   `SET status=RUNNING, locked_at, lease_owner, attempts+1 WHERE id IN (…)
   AND status=QUEUED`. Two workers can never execute the same execution
   (TEST 7) — the state transition is the lock; Redis is never the only
   guard (§59).
3. **Run** — nodes execute until WAIT/END/error or the per-cycle step budget
   (10 steps) is consumed; the execution continues next cycle.
4. **Stale-lease sweep** — `RUNNING AND locked_at < now − lease → QUEUED`
   recovers crashed workers (§77). Uncertain external sends are never blindly
   repeated: message actions are idempotent by `client_message_id`.

## Node execution (§58)

```
Load pinned workflow_version (§3/§60 — never the "current" definition)
 → build WorkflowContext + fresh entity snapshots (§34)
 → run node handler
 → write WorkflowExecutionStep (input/output snapshots — config + safe
   outputs only, never secrets, §33/§76)
 → advance current_node_id / schedule wait / classify error
```

Error classification (§37): `TRANSIENT` (retry with backoff), `PERMANENT`,
`CONFIGURATION` (needs correction), `PERMISSION` (fail safely + audit).
Retry never applies to unsubscribed/suppressed/invalid-configuration cases
(§38).

## Monitoring (§49–§50)

- `GET /api/v1/automation/executions` — filters: workflow, status, entity,
  trigger, date range; pagination.
- `GET /api/v1/automation/executions/{id}` — full step timeline with input/
  output snapshots, reasons for skips, errors.
- `GET /api/v1/automation/executions/counters` — real aggregates (§51).
- UI: `/automation/executions` + `/automation/executions/{id}` (polling
  refresh — no new realtime architecture, §71).

## Analytics foundation (§51)

Per-workflow aggregates computed from real rows: execution counts by status,
success/failure rates, average duration, last execution time. Exposed in the
workflows list/detail payloads; expanded further in Phase 10.

## Observability (§76)

Structured logs carry `workflow_id`, `workflow_version_id`, `execution_id`,
`trigger_event_id`, `entity_id`, `node_id`, `action`, `status`. Never logged:
API keys, provider tokens, passwords, cookies, message secrets. The intake
event log (`/api/v1/automation/events`) holds IDs + types only (§73 — no
unnecessary personal data copies).

## Failure recovery (§77)

Worker crash → stale-lease sweep requeues the execution → it resumes from
`current_node_id` on the PINNED version. If an action's external effect is
uncertain, idempotency keys prevent duplicate sends; nothing is blindly
repeated.
