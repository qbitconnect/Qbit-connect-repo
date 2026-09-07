# Automation & Workflow Engine (Phase 9)

> The workflow engine lets operators automate recurring decisions: **trigger →
> condition → action → wait → … → end**. It is built ON TOP of the existing
> services (leads, marketing, inbox) and NEVER bypasses their guards —
> eligibility, suppression, unsubscribe and provider rules are enforced by the
> very code paths automation reuses.

## Architecture

```
System Event (service-layer hook, best-effort)
   ↓
Event Dispatcher (app/automation/services/event_dispatcher.py)
   · WorkflowEvent row (unique event_id → idempotency, §13)
   · causation depth check + per-entity window limit (§40)
   · WorkflowExecution rows (QUEUED) for matching ACTIVE workflow versions
   ↓
Automation Worker (app/automation/workers/automation_worker.py)
   · scheduled triggers (single scheduler — the existing worker, §57)
   · ExecutionEngine: guarded-UPDATE claim (§59) → run nodes → persist (§58)
   ↓
Node Executors
   TRIGGER · CONDITION · BRANCH · ACTION · WAIT · END   (declarative only, §63)
   ↓
Execution history (steps with snapshots) → Analytics (§51) → Audit (§62)
```

Package layout (§1 — deliberately modular, no giant file):

```
app/automation/
  core/          exceptions · registry · schemas · context · workflow ·
                 trigger · condition · action · business_hours
  triggers/      definitions.py (21 triggers) · schedule.py
  conditions/    catalog.py (field catalog + entity snapshots)
  actions/       lead · conversation · communication · registry
  services/      event_dispatcher · workflow_service · execution_engine ·
                 analytics
  workers/       automation_worker.py
```

## Workflow lifecycle (§2, §46, §60)

```
DRAFT ──publish──▶ ACTIVE ──pause──▶ PAUSED ──resume──▶ ACTIVE
   │                  │                                 │
   │                  └──archive──▶ ARCHIVED ◀─archive──┘
   └─(edit: new draft version; publish: previous version → RETIRED)
```

- Publishing **validates** the full definition (§21): field catalog, operators,
  value types, action configs (DB-aware), graph structure (one trigger, all
  nodes reachable, no cycles, every path ends at END), node-count cap.
- **Versions are immutable after publication** (§60): editing an ACTIVE
  workflow creates a new draft version; publishing it retires the previous
  one. Running executions keep referencing the exact version row they started
  with (TEST 3) — behavior of a running execution never changes.

## Idempotency & loop protection (§13, §39, §40, §41)

| Mechanism | Where |
|---|---|
| `event_id` unique on `workflow_events` + `UNIQUE(workflow_id, trigger_event_id)` on executions | same event can start a given workflow exactly once |
| Causation depth cap (`QBIT_AUTOMATION_MAX_CAUSATION_DEPTH`, default 2) | events produced by automation actions carry `causation_id`; deep chains are blocked |
| Per-(workflow, entity) window limit (`QBIT_AUTOMATION_MAX_EXECUTIONS_PER_WINDOW` per `QBIT_AUTOMATION_WINDOW_MINUTES`) | blocks event storms |
| Graph acyclicity + max-steps-per-execution | publish-time and runtime |
| Action idempotency | ADD_TAG/ASSIGN are inherently idempotent; sends use deterministic `client_message_id = wf:{execution}:{node}` through the inbox reply idempotency |

## Communication actions (§26, §27, §53)

`send_email` / `send_whatsapp` go through `ReplyService.queue_reply` (Phase 8)
— the SAME path as human replies: account health, provider availability,
recipient validity, suppression gate, WhatsApp 24-hour window rule, outbox
delivery through the campaign provider registry. Automation **skips** the step
honestly (with a machine-readable reason) whenever a rule blocks the send —
it never bypasses one. Reasons include: `UNSUBSCRIBED`, `SUPPRESSED`,
`MISSING_EMAIL`, `MISSING_PHONE`, `LEAD_ARCHIVED`, `NO_CONVERSATION_CONTEXT`,
`WHATSAPP_WINDOW_CLOSED_TEMPLATE_REQUIRED`, `RECIPIENT_SUPPRESSED`.

`start_campaign` calls `CampaignService.request_launch` — the existing
validation-gated, idempotent launch path (§54). Validation failure → step
SKIPPED with the failing check names; already-launched campaigns skip with
`CAMPAIGN_ALREADY_…`.

## Scheduled triggers & delays (§11, §28–§30, §57)

- `SCHEDULED` trigger (daily HH:MM / hourly / interval / once) is evaluated by
  the automation worker using **slot-based deterministic event ids** — a slot
  never fires twice, no matter how often the worker cycles or restarts, and
  slots before `published_at` never backfill. Timezone-aware (never assumes
  one country's timezone).
- `WAIT` nodes persist `next_execution_at` and release the worker (never
  `sleep()`); state lives in the DB so delays survive restarts and crashes.
  Optional `respect_business_hours` shifts the wake time into the configured
  business window (§30).

## RBAC (§61)

Nine permissions: `automation.view · create · edit · publish · pause · resume
· execute · delete · view_executions`. ADMIN: all; MANAGER: all except
delete; OPERATOR: view/execute/view_executions; VIEWER: view +
view_executions. Enforced server-side on every route (and mirrored in the UI).

## Limits (§42)

`QBIT_AUTOMATION_MAX_NODES` (50) · `QBIT_AUTOMATION_MAX_STEPS_PER_EXECUTION`
(100) · `QBIT_AUTOMATION_MAX_RETRIES` (3, exponential backoff 30s→3600s) ·
`QBIT_AUTOMATION_MAX_WAIT_HOURS` (720) · `QBIT_AUTOMATION_BATCH_SIZE` (10) ·
`QBIT_AUTOMATION_LEASE_SECONDS` (300).

## API (§69)

`/api/v1/automation/workflows` (CRUD + validate/publish/pause/resume/
duplicate/archive/versions/from-template), `/api/v1/automation/executions`
(list/detail/cancel/retry/counters), `/api/v1/automation/templates`,
`/api/v1/automation/events`. See `docs/workflow-builder.md` for the UI and
`docs/workflow-execution.md` for the execution model.

## Realtime (§71)

No new realtime architecture: the execution monitor polls (consistent with
the existing inbox badge mechanism). Failed executions are visible in the
monitor and audited (`automation.execution_failed`).

## Non-goals honoured (§64, §83)

No arbitrary code/HTTP/shell nodes. No eval/exec/subprocess anywhere in the
engine — definitions are Pydantic-validated data with `extra="forbid"`.
`assign_team` is disabled by default (no team directory exists yet — honest
reserved capability, §24).
