# Email Delivery — Queue, Retry, Bounce & Complaint (Phase 7 §18–§26)

## Send pipeline (worker)

```
Campaign launch
  → audience snapshot (batched 500/loop, never all-in-RAM, §56)
  → eligibility chain (§15 reason codes)
  → recipients QUEUED (committed BEFORE broker push — DB-first)
  → marketing queue (Redis LIST qbit:queue:marketing, or in-process fallback)
  → worker marketing loop (emails/minute + backoff, §42 operational throttle)
  → EmailDeliveryService.process():
        claim QUEUED→SENDING (optimistic, race-safe)
        render + personalize + unsubscribe link (+ optional tracking rewrite)
        provider.send() — ONE recipient per call (§40: never bulk-CC)
        outcome → CampaignEvent + forward-only recipient transition
```

## Idempotency (§20)

`campaign_recipients.idempotency_key = campaign_id:recipient_id:message_version`
is UNIQUE. Worker restart / queue duplication / retry can never double-send.
A crash mid-send leaves `SENDING` — the startup recovery sweep marks it
`FAILED / SEND_STATE_UNKNOWN` instead of blindly resending: unknown provider
acceptance is never treated as safe-to-resend. Deliberate retries bump
`message_version` (POST /campaigns/{id}/requeue-failed).

## Retry policy (§22)

TRANSIENT failures retry with exponential backoff
(`base * 2^(attempt-1)`, capped, env-tunable) while
`attempts < max_attempts`. PERMANENT failures (invalid recipient, auth,
TLS, rejected) never retry. Provider RATE_LIMITED guidance is honored.

## Bounce handling (§24)

Webhook `bounce` events:

- **hard bounce** → recipient `BOUNCED` + **suppression** (HARD_BOUNCE) — no
  future email campaign can send to that address
- **soft bounce** → event recorded; provider-level transient failures retry
  under the normal policy; permanent bounces never loop

## Complaint handling (§25)

`complaint` events → recipient `COMPLAINED` + suppression (COMPLAINT) +
analytics update + audit trail. Complained recipients never receive further
marketing email.

## Delivery events (§26)

Provider events are normalized (`SENT DELIVERED BOUNCED COMPLAINED FAILED
OPENED CLICKED REPLIED UNSUBSCRIBED`) into `campaign_events` with a
forward-only recipient state machine. Out-of-order events (e.g. delivered
after read) are logged and ignored — analytics never double-count.

## Rate control (§42)

`QBIT_MARKETING_EMAILS_PER_MINUTE / …_PER_HOUR / …_CONCURRENCY` throttle
operational throughput. This is NOT an anti-ban/restriction-evasion mechanism
and must never be used as one.
