# Marketing Engine (Phase 5)

Phase 5 delivers the **marketing engine foundation** for QBIT Connect: the
reusable campaign infrastructure that future provider phases plug into.
It deliberately implements NO real messaging — WhatsApp/Email/SMS providers
are architecture-only until approved, provider-supported integrations land
(Phase 6+).

```
LEADS → SEGMENT (audience) → CAMPAIGN → CHANNEL → SENDER ACCOUNT → TEMPLATE
      → ELIGIBILITY → QUEUE → WORKER → PROVIDER → DELIVERY EVENTS
      → ANALYTICS → INBOX (future)
```

## Module layout

```
app/
  models/marketing.py          Campaign, CampaignRecipient, CampaignEvent,
                               CampaignTemplate, SendingAccount,
                               SuppressionEntry, OptOutRecord, CampaignQueueItem
  services/marketing/
    campaign.py                lifecycle + validation + launch pipeline
    audience.py                audience resolution + immutable snapshot
    eligibility.py             pre-queue checks (batched)
    suppression.py             global suppression + opt-out records
    template.py                safe {{variable}} engine
    queue.py                   DB-backed send queue (idempotency/retry/rate)
    events.py                  append-only event stream + provider normalizer
    analytics.py               metrics computed from real rows only
    worker.py                  worker-side send loop
    channels.py                WHATSAPP / EMAIL / SMS declarations
    providers/                 BaseMarketingProvider + channel interfaces
                               + MockProvider (TEST ONLY)
  api/v1/{campaigns,templates,sending_accounts,suppression}.py
  ui/campaigns.py              server-rendered control center
  templates/campaigns/*.html
```

## Database

Eight additive tables (migration `0004_marketing_foundation`, forward-only,
non-destructive):

| Table | Purpose | Key constraints |
|---|---|---|
| `campaigns` | campaign definition + platform-level status | indexes: status+created, channel, scheduled_at |
| `campaign_recipients` | launch-time audience snapshot | UNIQUE (campaign_id, lead_id) |
| `campaign_events` | immutable event stream | indexes: campaign+created, type+created |
| `campaign_templates` | per-channel templates | channel+status index |
| `sending_accounts` | multi-sender abstraction | no secret columns |
| `suppression_entries` | global do-not-contact | UNIQUE (type, address, channel_key) |
| `opt_out_records` | unsubscribe evidence | UNIQUE (channel_key, address) |
| `campaign_queue` | send queue | UNIQUE (campaign_id, recipient_id, message_version) — the idempotency key |

`channel_key` normalizes NULL channel to `''` so uniqueness behaves
identically on PostgreSQL and SQLite.

## Campaign lifecycle

```
DRAFT ──launch──▶ QUEUED ──worker: snapshot+eligibility+queue──▶ RUNNING
   │                  │                                            │
   └─schedule──▶ SCHEDULED ─(due, worker)──▶ QUEUED       pause ──▶ PAUSED
                                                            resume ─▶ RUNNING
RUNNING (queue drained) ──▶ COMPLETED
any of SCHEDULED/QUEUED/RUNNING/PAUSED ──cancel──▶ CANCELLED
COMPLETED/CANCELLED/FAILED/DRAFT ──archive──▶ ARCHIVED
```

- Statuses are **platform-level** — never provider-specific states.
- The HTTP layer only validates and flips status; heavy work runs in the
  worker process (`CampaignWorker.process_cycle`), which also performs
  scheduled-campaign promotion and campaign completion detection.
- Launch is blocked unless validation passes AND at least one recipient is
  eligible AND the sending account's provider is configured.

## Audience snapshots (reproducibility)

The audience definition (saved view / filter grammar / tags / explicit ids /
statuses) is resolved at launch into `campaign_recipients` rows in bounded
batches (bulk `INSERT`). After the snapshot, changes to saved views or leads
never alter a launched campaign. A double-snapshot is refused.

## Queue, retry, idempotency, rate control

- **Queue**: DB rows are the source of truth; the worker claims WAITING items
  with a guarded UPDATE lease (stale leases recovered to WAITING).
- **Idempotency**: UNIQUE (campaign_id, recipient_id, message_version);
  duplicate enqueues are `ON CONFLICT DO NOTHING` no-ops. A provider send is
  keyed `campaign_id:recipient_id:message_version`.
- **Retry**: failures are classified TRANSIENT (exponential backoff, bounded
  attempts) vs PERMANENT/CONFIGURATION (immediate FAILED, never retried).
- **Rate control**: conservative per-account throttling
  (default 10/min, 100/hour, overridable via `config_metadata.rate_policy`).
  This is operational pacing for our own sends ONLY — the codebase contains
  no anti-ban, stealth, fingerprint, CAPTCHA or rate-limit-evasion logic and
  none may be added.

## Provider architecture

`BaseMarketingProvider` defines the contract: `validate_configuration`,
`validate_recipient`, `validate_message`, `send`, `get_status`,
`handle_event`, `health_check`. The registry registers WhatsApp/Email/SMS
**interfaces** (every real operation fails honestly with
`PROVIDER_NOT_CONFIGURED`) plus the MOCK provider in test environments only.
The mock provider can never register in production; UI/API label it
`MOCK / TEST ONLY`.

## Security

- RBAC enforced server-side on every endpoint (`campaigns.*`, `templates.*`,
  `sending_accounts.*`, `suppression.*`; legacy `campaign.*` codes kept).
- Provider secrets are never accepted or stored in Phase 5 (credential vault
  arrives later); secret-like config keys are rejected, and responses use
  `to_public_dict()` which omits raw config.
- Event payloads pass through the same secret-redaction as the audit log.
- Templates cannot execute expressions: only `{{identifier}}` substitution
  from an allowlisted lead-field map; malformed blocks are rejected.
- All state changes are audit-logged (`campaign.*`, `template.*`,
  `sending_account.*`, `suppression.*` actions).

## Observability

Structured events (request_id-bound): `campaign_created`, `campaign_validated`,
`campaign_started`, `campaign_paused`, `campaign_resumed`,
`campaign_cancelled`, `campaign_completed`, `recipient_queued`,
`recipient_sent`, `recipient_failed` — with campaign_id/recipient_id/
provider/sending_account_id and never secrets.

## Configuration (defaults are conservative)

| Setting | Default | Purpose |
|---|---|---|
| `QBIT_MARKETING_ALLOW_MOCK_PROVIDER` | false | register MOCK provider (non-prod only) |
| `QBIT_MARKETING_SNAPSHOT_BATCH_SIZE` | 1000 | snapshot/eligibility batch size |
| `QBIT_MARKETING_QUEUE_BATCH_SIZE` | 25 | queue items claimed per cycle |
| `QBIT_MARKETING_RATE_PER_MINUTE` / `_PER_HOUR` | 10 / 100 | default rate policy |
| `QBIT_MARKETING_MAX_ATTEMPTS` | 3 | retry ceiling |
| `QBIT_MARKETING_RETRY_BASE_SECONDS` / `_MAX_SECONDS` | 30 / 7200 | backoff window |
| `QBIT_MARKETING_MAX_AUDIENCE` | 100000 | launch safety cap |
