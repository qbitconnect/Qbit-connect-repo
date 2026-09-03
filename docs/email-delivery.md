# Email Delivery Pipeline (Phase 7)

How an EMAIL campaign moves from launch to delivery — and what happens when providers report back.

## Flow

```
Campaign
 ↓ audience snapshot (immutable, batched)
Eligibility  (email valid → opt-in → suppression → unsubscribe)
 ↓ queue (DB-backed, idempotent per campaign+recipient+version)
Email Worker  (never synchronous from an HTTP request)
 ↓ render + sanitize + unsubscribe link + opt-in tracking
EmailProvider (smtp | email_api)
 ↓ individual recipient delivery (one To:, never CC)
Provider → SENT → DELIVERED / BOUNCED / COMPLAINT → events
```

## Eligibility reasons (§15)

`MISSING_EMAIL`, `INVALID_EMAIL`/`INVALID_ADDRESS`, `NO_OPT_IN`, `UNSUBSCRIBED`, `SUPPRESSED`, `INVALID_TEMPLATE`, `ACCOUNT_UNAVAILABLE`, `PROVIDER_NOT_READY` — plus the launch-time gate `EMAIL_SENDER_UNHEALTHY`.

A publicly scraped email address is **not** marketing consent. Opt-in evidence must exist in lead metadata (`marketing_opt_in` or `opt_in_status`), and it is never fabricated. Suppressed addresses are skipped with a reason and never enter the provider queue.

## Launch gates (§37, §41)

Before launch the validation report checks: channel, audience, template ACTIVE, sending account ACTIVE **and channel-matching**, `EMAIL_SENDER_UNHEALTHY`, provider configured, email-specific template requirements, **unsubscribe configuration** (a real link must be generatable) and schedule. Failed validation blocks launch with an actionable report.

## Idempotency (§20)

The logical send is `UNIQUE (campaign_id, recipient_id, message_version)`. Worker restarts, queue duplication and retries cannot create a second email. A timeout whose acceptance state is unknown is classified `DELIVERY_STATE_UNKNOWN` and **never retried** — a duplicate email is worse than an honest failure.

## Retry policy (§22)

- TRANSIENT (connection errors, 4xx, rate limits) → exponential backoff, capped; `retry_after` hints honored
- PERMANENT (invalid recipient, auth, rejected) → FAILED immediately, never retried
- per-account operational throttling (`rate_policy`: `messages_per_minute/hour` or `emails_per_minute/hour`) — throttling our own pace, never evading provider limits

## Bounce handling (§24)

| Type | Recipient | Suppression |
|---|---|---|
| HARD_BOUNCE (user unknown…) | FAILED + `bounced_at` | **EMAIL suppressed** (reason BOUNCED) — no future sends |
| SOFT_BOUNCE (mailbox full…) | event recorded | none — queue policy governs any redelivery |

Unclassified bounces are treated conservatively as soft (recorded, never fabricated into hard).

## Complaint handling (§25)

A spam complaint records `MESSAGE_COMPLAINED` (+`complained_at`), **suppresses the address** (reason COMPLAINT) from all future marketing and updates analytics. Complaint recipients never continue receiving campaigns.

## Event vocabulary (§26)

`MESSAGE_SENT → MESSAGE_DELIVERED → MESSAGE_READ/OPENED → MESSAGE_REPLIED`, plus `MESSAGE_BOUNCED`, `MESSAGE_COMPLAINED`, `MESSAGE_CLICKED`, `MESSAGE_UNSUBSCRIBED`, `MESSAGE_FAILED`. Recipient status moves **forward only** (state machine, Phase 6 §21); every event appends an immutable `CampaignEvent`.

## Analytics (§34)

Recipients / eligible / skipped / queued / sent / delivered / bounced (hard+soft) / complained / opened / clicked / replied / unsubscribed + delivery, bounce, complaint, open, click, reply and unsubscribe rates — **all computed from actual events** (never fabricated). Open/click numbers are explicitly reported as directional only: email clients block or prefetch tracking pixels.

Endpoint: `GET /api/v1/campaigns/{id}/email/analytics` (permission `campaigns.email.analytics`), also rendered on the campaign dashboard for EMAIL campaigns.

## Performance (§56)

- audience snapshot + eligibility + queueing run in bounded batches (1000/cycle by default) — a 100k-recipient campaign never loads all leads into RAM
- queue inserts are bulk `ON CONFLICT DO NOTHING`
- worker claims bounded batches with lease recovery
- analytics aggregate over indexed columns
