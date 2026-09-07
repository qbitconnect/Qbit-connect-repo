# QBIT CONNECT — PHASE 9 AUDIT
## Repository & Phase 1–8 Implementation Audit (STEP 0)

Date: 2026-09-07 · Codebase version: 0.8.0 · Scope: prerequisites for the Automation & Workflow Engine

---

## 1. Reusable Event Architecture

There is **no generic event bus / dispatcher / Redis pub/sub** in the codebase. Events are
**append-only DB rows written synchronously by the service layer**:

| Trail | Model / table | Written by | Vocabulary |
|---|---|---|---|
| Campaign & message lifecycle | `CampaignEvent` (`campaign_events`) | `EventService.record` (`services/marketing/events.py`) — single choke point | `EventType`: CAMPAIGN_CREATED/VALIDATED/QUEUED/STARTED/PAUSED/RESUMED/CANCELLED/COMPLETED/FAILED, RECIPIENT_ADDED/SKIPPED, MESSAGE_QUEUED/SENT/DELIVERED/READ/REPLIED/FAILED/BOUNCED/COMPLAINED/OPENED/CLICKED/UNSUBSCRIBED |
| Lead activity | `LeadActivity` (`lead_activities`) | `LeadActivityService.log` via `LeadWorkspaceService` (`create_lead`, `apply_update`, `set_status`, `add_note`), `TagService`, `LeadIngestionService` | `lead_created`, `status_changed`, `tag_added`, `tag_removed`, `note_added`, `lead_updated`, `EVENT_SCRAPED`, `lead_archived`… |
| Conversation activity | `ConversationEvent` (`conversation_events`) | `ConversationEngine` (`services/inbox/engine.py`) — ~10 inline `session.add(ConversationEvent(...))` sites | `CONVERSATION_CREATED`, `MESSAGE_RECEIVED`, `MESSAGE_SENT`, `MESSAGE_FAILED`, `STATUS_CHANGED`, `PRIORITY_CHANGED`, `ASSIGNED`, `UNASSIGNED`, `LEAD_LINKED`, `CONVERSATION_REOPENED`, `NOTE_ADDED`… |
| Scrape job events | `ScrapeJobEvent` (`scrape_job_events`) | `JobRunner` (`services/scraping/runner.py`) | `JOB_COMPLETED`, … |
| Webhook intake dedup | `ProviderEvent` (`provider_events`, UNIQUE(provider, provider_event_id)) | WhatsApp + Email webhook services | provider ids |

**Phase 9 design decision:** the workflow engine introduces a thin **intake dispatcher**
(`automation/services/event_dispatcher.py`) that is *called from* these existing service
choke points. It only (a) records a deduplicated `WorkflowEvent` row and (b) creates
`WorkflowExecution` rows (QUEUED) for matching ACTIVE workflows. **No workflow body ever
executes inside HTTP handlers** (§12) — execution happens exclusively in the worker loop.
All dispatch calls are best-effort (wrapped, never break the primary flow — same pattern
as `AuditService`).

### Hook points (all verified)
- `LeadWorkspaceService.create_lead` → `lead.created`; `apply_update` → `lead.updated` (only when changed); `set_status` → `lead.status_changed`
- `TagService.assign` (created=True) → `lead.tag_added`; `unassign` (removed) → `lead.tag_removed`
- `LeadIngestionService.ingest` (created=True) → `lead.scraped` (source_type=scraper) / `lead.imported` (source_type=import)
- `EventService.record` → maps `CAMPAIGN_COMPLETED/FAILED`, `MESSAGE_REPLIED→campaign.recipient.replied`, `MESSAGE_FAILED→campaign.recipient.failed` + `message.sent/delivered/failed`, `MESSAGE_SENT→message.sent`, `MESSAGE_DELIVERED→message.delivered`
- `ConversationEngine.ingest_inbound` → `conversation.created`, `conversation.reopened`, `conversation.inbound_message`, `message.received`; `change_status` → `conversation.status_changed`; `assign_user` → `conversation.assigned`
- `JobRunner._finish_success` → `scrape.job.completed` (§55 event prepared)
- `SCHEDULED` trigger → computed by the worker from workflow definitions (no second scheduler)

## 2. Existing Queues

- **Scrape queue (Redis, optional):** `qbit:queue:scrape` LIST, `qbit:queue:scrape:scheduled` ZSET, `qbit:job:{id}:control|lease` — used only by the scrape engine (`services/scraping/queue.py`). Falls back to `InProcessQueueBackend` when `REDIS_URL` is unset.
- **Campaign send queue (Postgres):** `campaign_queue` — `QueueService.claim_batch` uses **guarded UPDATE leases** (`status IN (WAITING,RETRY) AND available_at<=now` → `PROCESSING, locked_at, lease_owner, attempts+1`), stale-lease sweep, exponential backoff on TRANSIENT errors (`min(base*2^(attempts-1), max)`), idempotency `UNIQUE(campaign_id, recipient_id, message_version)` via ON CONFLICT DO NOTHING.
- **Inbox outbox (Postgres):** `inbox_outbox` — same optimistic lease pattern (`OutboxService._claim_batch`), `idempotency_key UNIQUE = inbox:{conversation_id}:{client_message_id}`.
- **Import/export batches:** `leased_at/lease_owner` columns, same idea.

**Phase 9 decision:** executions are claimed with the **same Postgres guarded-UPDATE lease
pattern** (§59: do not rely only on Redis). A new table-backed queue is unnecessary —
`workflow_executions` itself is the queue (`status=QUEUED` + `next_execution_at` +
`locked_at/lease_owner`).

## 3. Existing Workers

`app/worker.py` (`python -m app.worker`, compose service `qbit-worker`) runs 5 isolated
asyncio loops: `_periodic_sweep`, `_data_jobs_loop` (Phase 4), `_campaign_loop`
(`CampaignWorker.process_cycle`), `_inbox_outbox_loop` (`OutboxService.process_cycle`),
plus the main scrape dequeue loop. Each loop: `async with self.db.session()` →
`process_cycle(session)` → sleep poll (short when work was done, longer when idle),
wrapped in keep-alive try/except (isolation rule §15).

**Phase 9 adds one more isolated loop:** `_automation_loop` → `AutomationWorker.process_cycle`
(failures never touch other loops).

## 4. Existing Scheduling

No beat/cron scheduler exists. All periodic work is **polling loops gated by DB
timestamps**: `Campaign.scheduled_at` (+ `process_due_schedules`), `available_at`
(queue retries), `locked_at` leases. Phase 9 follows the identical pattern:
- `SCHEDULED` triggers: worker computes due ticks from ACTIVE workflow definitions (single scheduler — the existing worker).
- `WAIT` nodes: `workflow_executions.next_execution_at` persisted; the worker requeues due WAITING rows. Delayed state survives restart/crash by construction (DB is the source of truth).

## 5. Existing Campaign Actions (reusable)

- `CampaignService.validate(session, id)` → read-only validation report (channel, audience, template ACTIVE, sending account ACTIVE+healthy+capabilities, provider config, template requirements, batched eligibility preview, unsubscribe URL, schedule).
- `CampaignService.request_launch` (HTTP entry, §26): status guard DRAFT/SCHEDULED → validate → requires `eligible > 0` → `QUEUED` (SEND_NOW) or armed (SCHEDULED). **Idempotent & safe to call programmatically** (status guard blocks re-entry; double-snapshot guard in `process_launch`; queue ON CONFLICT idempotency).
- `CampaignService.process_launch` (worker side): audience snapshot → eligibility pass → enqueue → RUNNING. Heavy work is worker-only.
- `AudienceService.snapshot()`, `EligibilityService.check_batch`, `SuppressionService.is_suppressed/check_batch`, `QueueService.enqueue` — all reusable as-is.

**START_CAMPAIGN safety verdict (§54):** safely implementable — the two-stage launch
design already exists precisely for non-human launch requests. The automation action will
call `request_launch` (never `process_launch`) from the worker context with
`actor_id = workflow.created_by`; validation failure → step SKIPPED with the report
reasons (never partial launch). Guarded by `QBIT_AUTOMATION_ENABLE_START_CAMPAIGN` (default true) and documented.

## 6. Existing Conversation Actions (reusable)

`ConversationEngine`: `change_status`, `change_priority`, `assign_user`, `add_note`,
`link_lead`, `get_or_create_conversation`, `within_whatsapp_window`.
`ReplyService.queue_reply(session, conversation, *, user_id, body, subject,
client_message_id, template_id, ...)` — **the communication path to reuse** (§26):
validates account health, provider availability, recipient address, **suppression gate**
(raises `ConflictError("RECIPIENT_SUPPRESSED:…")`), **WhatsApp 24h window rule**
(TemplateRequiredError → template fallback), creates `Message(status=SENDING)` +
`InboxOutboxItem(idempotency_key=inbox:{conversation_id}:{client_message_id})`; delivery
through the SAME provider abstraction as campaigns in the outbox loop.

## 7. Missing Workflow Functionality (gap list)

1. No Workflow/WorkflowVersion/WorkflowExecution/Step/Event models or tables.
2. No trigger→workflow matching, no condition engine, no action framework, no delay state machine.
3. No event intake with end-to-end idempotency (dedup exists only at provider-webhook level).
4. No causation/correlation tracking for automation-caused mutations (loop protection).
5. No automation RBAC (`automation.*`), no automation audit actions, no UI, no API, no docs, no version bump.

## 8. Required Database Changes (additive only)

New tables (migration `0008_automation_workflows`, batch-mode safe, String statuses per codebase convention, PortableJSON):
- `workflows` (status/trigger_type indexed)
- `workflow_versions` (UNIQUE(workflow_id, version))
- `workflow_executions` (status/entity/next_execution_at/created_at indexed; **UNIQUE(workflow_id, trigger_event_id)** = event idempotency §13; lease columns §59)
- `workflow_execution_steps` (execution_id+status indexed)
- `workflow_events` (event_id UNIQUE, created_at indexed) — intake log & causation record
- 9 new permissions seeded (`NEW_PERMISSIONS`/`PERMISSION_MATRIX` raw-SQL pattern from 0007) + `AUTOMATION_TABLES` added to `tests/test_migrations.py` whitelist.

**No destructive operations. No changes to existing tables.**

## 9. Security Risks & Mitigations

| Risk | Mitigation (implemented in Phase 9) |
|---|---|
| Arbitrary code/expr execution via definitions (§63) | Fully declarative schema; strict node/field/operator/action catalogs; no eval/exec/subprocess anywhere; `extra` fields rejected by Pydantic models |
| Template/variable injection (§35) | `{{path}}` allowlist substitution against loaded entity snapshots only — regex-validated path grammar, HTML-escaped by Jinja2 in UI, plain text elsewhere |
| Infinite loops (§40) | Causation-depth cap, per-entity+workflow window limit, max-steps/execution, graph acyclicity at publish, node-count cap |
| Duplicate executions/messages (§13, §39) | UNIQUE(workflow_id, trigger_event_id); deterministic `client_message_id=wf:{execution_id}:{node_id}` through ReplyService idempotency |
| Two workers double-executing (§59) | Guarded-UPDATE claim (Postgres), lease + stale sweep; status re-checked before every node |
| RBAC bypass / unauthorized publish/execute (§61) | `require_permission` on every route; publish gate = `automation.publish`; execution endpoints guarded; UI mirrors via `require_ui_permission` |
| IDOR | All detail routes 404 on missing/unpermitted (no existence leak) — existing convention |
| Secrets in logs/snapshots (§76, §73) | Snapshots store IDs + config + safe outputs only; provider responses summarized, never raw; redact() reused |
| Oversized workflows (§65) | max_nodes validation + Pydantic bounds on definition size |
| SQL injection | ORM-only parameterized queries (existing convention) |

## 10. Implementation Plan

1. `app/models/automation.py` + registration; migration `0008_automation_workflows` (additive).
2. `app/automation/` package: `core/` (exceptions, registry, schemas, context, workflow graph, trigger/condition/action bases, execution engine), `triggers/`, `conditions/`, `actions/`, `services/` (event_dispatcher, workflow_service, execution_service), `workers/automation_worker.py`.
3. Settings `QBIT_AUTOMATION_*` in `core/config.py`; version bump 0.8.0 → 0.9.0.
4. RBAC: 9 `automation.*` permissions (rbac.py + migration seed).
5. Event hooks in the 9 service choke points listed in §1 (best-effort, lazy import).
6. API `app/api/v1/automation.py` (15 endpoints, page envelopes, audit on mutations).
7. Worker loop `_automation_loop` in `app/worker.py`.
8. UI `app/ui/automation.py` + 5 templates (list / builder / detail / executions / execution detail), nav item in `base.html`.
9. Tests `tests/automation/` (spec §74–§75 coverage incl. security) + `AUTOMATION_TABLES` migration whitelist + `scripts/phase9_smoke.py`.
10. Docs: `docs/automation.md`, `workflow-builder.md`, `workflow-triggers.md`, `workflow-conditions.md`, `workflow-actions.md`, `workflow-execution.md`, `automation-security.md`; README index; version 0.9.0.
