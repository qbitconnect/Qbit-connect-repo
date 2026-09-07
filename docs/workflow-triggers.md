# Workflow Triggers (Phase 9 §6–§11)

Triggers map normalized **system events** to workflow starts. They are pure
matchers — they never execute anything. A workflow has exactly one trigger.

## Event flow

```
service-layer hook (best-effort, never breaks business flow)
  → dispatcher records WorkflowEvent (unique event_id)
  → matches ACTIVE workflows by trigger type + trigger config filter
  → creates QUEUED executions
```

## Lead triggers (§7)

| Trigger | Fires on | Config filter |
|---|---|---|
| `LEAD_CREATED` | `lead.created` (manual create) | — |
| `LEAD_UPDATED` | `lead.updated` (field edit with changes) | — |
| `LEAD_STATUS_CHANGED` | `lead.status_changed` | `statuses: ["QUALIFIED", …]` (optional) |
| `LEAD_TAG_ADDED` | `lead.tag_added` | `tag: "Hot"` (optional, case-insensitive) |
| `LEAD_TAG_REMOVED` | `lead.tag_removed` | `tag: "…"` (optional) |
| `LEAD_IMPORTED` | `lead.imported` (import batch ingestion) | — |
| `LEAD_SCRAPED` | `lead.scraped` (scraper pipeline ingestion) | — |

Example trigger config:

```json
{"type": "LEAD_TAG_ADDED", "tag": "Hot"}
```

## Campaign triggers (§8)

| Trigger | Fires on (CampaignEvent → automation event) |
|---|---|
| `CAMPAIGN_COMPLETED` | `CAMPAIGN_COMPLETED` → `campaign.completed` |
| `CAMPAIGN_FAILED` | `CAMPAIGN_FAILED` → `campaign.failed` |
| `CAMPAIGN_RECIPIENT_REPLIED` | `MESSAGE_REPLIED` → `campaign.recipient.replied` |
| `CAMPAIGN_RECIPIENT_FAILED` | `MESSAGE_FAILED` → `campaign.recipient.failed` |

Context refs: `campaign_id`, `recipient_id`, `lead_id` (and `message_id` +
`conversation_id` for message-kind events when the message row is resolvable).

## Conversation triggers (§9)

| Trigger | Fires on |
|---|---|
| `INBOUND_MESSAGE` | `conversation.inbound_message` (any inbound WhatsApp/Email message) |
| `CONVERSATION_CREATED` | `conversation.created` |
| `CONVERSATION_ASSIGNED` | `conversation.assigned` |
| `CONVERSATION_STATUS_CHANGED` | `conversation.status_changed` — filter: `status: "OPEN"` |
| `CONVERSATION_REOPENED` | `conversation.reopened` (reply on RESOLVED/CLOSED thread) |

Example (§56): inbound message contains "price" → assign sales → priority HIGH:

```json
{"type": "INBOUND_MESSAGE"}
```

with a CONDITION node `message.body contains price`.

## Message triggers (§10)

Normalized, provider-independent: `MESSAGE_RECEIVED`, `MESSAGE_SENT`,
`MESSAGE_DELIVERED`, `MESSAGE_FAILED`. No provider-specific logic ever enters
the engine — webhooks are normalized by the existing webhook services first.

## Scheduled trigger (§11)

```json
{"type": "SCHEDULED", "schedule_type": "daily", "time": "09:30", "timezone": "Asia/Kolkata"}
{"type": "SCHEDULED", "schedule_type": "hourly", "timezone": "UTC"}
{"type": "SCHEDULED", "schedule_type": "interval", "interval_minutes": 90}
{"type": "SCHEDULED", "schedule_type": "once", "run_at": "2026-09-10T09:00:00+05:30"}
```

- Uses the existing worker as the ONLY scheduler (§57) — no second scheduler.
- Slot-based idempotency: each slot fires at most once, restart-safe.
- Slots before `published_at` never fire (no backfill on publish day).
- Timezone is configurable (IANA names) — never assumed.

## Scrape event (§55 — prepared)

`SCRAPE_JOB_COMPLETED` fires on `scrape.job.completed` with the job id and
record counts. Workflows may tag/annotate created leads; they can never
launch a campaign without the full eligibility gate (`start_campaign` uses
the validated launch path).
