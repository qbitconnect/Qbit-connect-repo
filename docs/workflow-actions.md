# Workflow Actions (Phase 9 §22–§27, §54)

Actions are modular units with `validate_config()` (publish-time) and
`execute()` (runtime, structured output §36). All actions reuse existing
services so idempotency/normalization/activity behaviour matches manual
edits. Automation-caused changes carry `user_id=None` and are traceable via
the execution step + causation chain (§41).

## Lead actions (§23)

| Key | Config | Notes |
|---|---|---|
| `add_tag` | `{"tag": "Hot"}` | idempotent — second run creates nothing (§39) |
| `remove_tag` | `{"tag": "Cold"}` | missing tag → success no-op |
| `change_status` | `{"status": "QUALIFIED"}` | validates against the lead status vocabulary |
| `update_lead` | `{"fields": {"city": "Surat"}}` | whitelisted fields only (`contact_name, first_name, last_name, address, city, state, country, category, industry, website`) — identity fields (email/phone/business_name) are NOT automation-editable, provenance stays honest |
| `add_note` | `{"content": "Hello {{lead.contact_name}}"}` | variables rendered (§35), activity-logged |

## Assignment actions (§24)

| Key | Config | Notes |
|---|---|---|
| `assign_user` / `assign_conversation` | `{"user_id": "<uuid>"}` | target must exist; vanished target → SKIPPED `ASSIGN_TARGET_USER_NOT_FOUND` |
| `unassign_conversation` | `{}` | idempotent no-op when already unassigned |
| `assign_team` | `{"team_id": "<uuid>"}` | **disabled by default** — no team directory exists yet (Phase 8 reserved column); enable with `QBIT_AUTOMATION_ENABLE_TEAM_ASSIGN=true` |

Assignment actions target conversations (the only assignable entity in the
current data model).

## Conversation actions (§25)

| Key | Config |
|---|---|
| `change_conversation_status` | `{"status": "OPEN"}` (PENDING/OPEN/WAITING/RESOLVED/CLOSED) |
| `change_priority` | `{"priority": "URGENT"}` (NORMAL/HIGH/URGENT) |
| `add_internal_note` | `{"content": "…"}` — internal note, never sent to the customer |

## Communication actions (§26, §27, §53)

| Key | Config |
|---|---|
| `send_whatsapp` | `{"body": "Hi {{lead.first_name}}…"}` or `{"template_id": "<uuid>"}` |
| `send_email` | `{"body": "…"}`, optional `{"subject": "…"}` or `{"template_id": "…"}` |

Safety ladder (checked before every send, in order — §27):

1. conversation context (lead's most recent conversation; none → SKIPPED
   `NO_CONVERSATION_CONTEXT`)
2. recipient address present (`MISSING_EMAIL` / `MISSING_PHONE`)
3. lead not archived (`LEAD_ARCHIVED`)
4. suppression / unsubscribe (`UNSUBSCRIBED` / `SUPPRESSED`) — via
   `SuppressionService.is_suppressed`, address keys checked first
5. WhatsApp 24-hour customer-service window — outside the window a template
   is required (`WHATSAPP_WINDOW_CLOSED_TEMPLATE_REQUIRED`); the provider rule
   is never bypassed
6. queue through `ReplyService.queue_reply` → outbox worker → provider
   (same architecture as campaigns; §26 flow diagram honoured)

Idempotency (§39, §77): the deterministic `client_message_id =
wf:{execution_id}:{node_id}` makes retries return the existing message —
duplicate sends are impossible.

## Campaign action (§54)

| Key | Config | Behaviour |
|---|---|---|
| `start_campaign` | `{"campaign_id": "<uuid>"}` | calls `CampaignService.request_launch` (validate → QUEUED). Disabled with `QBIT_AUTOMATION_ENABLE_START_CAMPAIGN=false`. Validation failure → SKIPPED `CAMPAIGN_VALIDATION_FAILED:<checks>`; already launched → SKIPPED `CAMPAIGN_ALREADY_…`. No duplicate recipients (snapshot guard), no eligibility bypass. |

## Skips vs failures (§27, §37)

- **SKIPPED** (step status SKIPPED + reason): a rule legitimately blocks this
  action for this entity — the workflow continues along its path.
- **FAILED / retry**: transient errors (`TRANSIENT` class) back off
  exponentially (§38); permanent/configuration/permission errors fail the
  execution with a classified error (§37). Never retried: invalid lead,
  unsubscribed, suppressed, invalid configuration.
