# Campaigns

Campaigns orchestrate one message to one audience over one channel through
one sending account with one template.

## Campaign model

| Field | Notes |
|---|---|
| `name`, `description` | identity |
| `channel` | `WHATSAPP` \| `EMAIL` \| `SMS` |
| `status` | DRAFT → VALIDATING → SCHEDULED → QUEUED → RUNNING → PAUSED → COMPLETED / CANCELLED / FAILED / ARCHIVED |
| `audience_definition` | JSON (see below) |
| `template_id` | must match the campaign channel |
| `sending_account_id` | must match the campaign channel |
| `schedule_type` | `SEND_NOW` \| `SCHEDULED` (+ `scheduled_at`, `timezone`) |
| `validation_report` | cached output of the latest validation |

## Audience definitions

```jsonc
{"type": "saved_view", "saved_view_id": "...", "statuses": ["NEW"]}
{"type": "filters", "filters": {"field": "city", "op": "eq", "value": "Surat"}}
{"type": "tags", "tags": ["vip", "hot"], "match": "any"}   // or "all"
{"type": "selected", "lead_ids": ["...", "..."]}
```

All types resolve through the Phase 4 validated filter engine (whitelisted
fields/operators only) and exclude archived/merged leads. PRIVATE saved views
are usable only by their owner.

## Validation report

`POST /api/v1/campaigns/{id}/validate` returns:

```json
{
  "ok": true,
  "checks": {"channel": {"status": "PASS"}, "audience": {...}, "template": {...},
              "sending_account": {...}, "provider": {...}, "schedule": {...}},
  "audience_count": 10000,
  "eligibility": {"eligible": 8421, "skipped": 1579, "suppressed": 320,
                   "missing_address": 1259, "no_opt_in": 0}
}
```

Launch (`POST .../launch`) requires `ok == true` and `eligible > 0`; the
launch itself only flips status — the worker performs the snapshot,
eligibility pass and queueing. Small launches are also processed inline by
the UI for immediacy, using the identical service path.

## API surface

```
GET    /api/v1/campaigns                       list (status/channel/search filters)
POST   /api/v1/campaigns                       create (DRAFT)
GET    /api/v1/campaigns/dashboard             dashboard totals
GET    /api/v1/campaigns/{id}                  detail
PATCH  /api/v1/campaigns/{id}                  edit (DRAFT/SCHEDULED only)
POST   /api/v1/campaigns/{id}/validate         validation report
POST   /api/v1/campaigns/{id}/launch           arm + queue (gated)
POST   /api/v1/campaigns/{id}/pause            RUNNING → PAUSED
POST   /api/v1/campaigns/{id}/resume           PAUSED → RUNNING
POST   /api/v1/campaigns/{id}/cancel           cancel pending work (history intact)
POST   /api/v1/campaigns/{id}/archive          terminal states → ARCHIVED
GET    /api/v1/campaigns/{id}/recipients       snapshot rows (paged)
GET    /api/v1/campaigns/{id}/recipients/{rid} recipient detail (lead ref, queue, events)
GET    /api/v1/campaigns/{id}/events           event stream (paged)
GET    /api/v1/campaigns/{id}/analytics        metrics + rates
POST   /api/v1/campaigns/events/provider       generic provider-event ingestion (§37)
```

## UI

- `/campaigns` — dashboard cards (totals by status) + campaign table with
  real per-campaign numbers.
- `/campaigns/new` — 9-step wizard: details → channel → audience → template →
  sending account → eligibility preview → schedule → review → launch. The
  eligibility preview reports exact eligible/skipped/suppressed/missing/
  no-opt-in counts before anything is created.
- `/campaigns/{id}` — overview, recipients table, event timeline, action
  buttons gated by RBAC and campaign status.
- Empty states are honest: "No sending provider configured", "No campaigns
  yet" — never demo data (brief §35).

## Recipients & events

Recipient rows carry the full delivery timeline as separate timestamps
(`queued_at`, `sent_at`, `delivered_at`, `read_at`, `replied_at`,
`failed_at`) and are only ever moved FORWARD through the status order.
Events are append-only; cancellation cancels pending work but never rewrites
completed provider events.
