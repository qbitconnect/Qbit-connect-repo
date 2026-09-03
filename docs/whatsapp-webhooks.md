# WhatsApp Webhooks (Phase 6 §17–§24)

Delivery receipts and inbound messages arrive through the official webhook
mechanism. QBIT Connect processes them securely and idempotently.

## Endpoints (§19)

```
GET  /api/v1/webhooks/whatsapp   subscription verification (hub.challenge)
POST /api/v1/webhooks/whatsapp   event delivery
```

These routes are intentionally **not** JWT-authenticated — they are called by
the provider. They are secured the way the provider requires (below).

## Configuration (Meta App side)

1. Webhook callback URL: `https://your-qbit-host/api/v1/webhooks/whatsapp`
2. Verify token: the value of `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
3. Subscribe to the `messages` field on the WABA(s)/phone number(s)
4. Set `WHATSAPP_APP_SECRET` (Meta App settings → App secret)

Production boot refuses to start without `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
(config validation), and POST refuses to process anything without
`WHATSAPP_APP_SECRET` — unverified events are never accepted (§18).

## Security (§18)

- **GET verification**: `hub.mode == "subscribe"` and constant-time comparison
  of `hub.verify_token` against the configured token; only then is
  `hub.challenge` echoed.
- **Signature validation**: every POST requires `X-Hub-Signature-256` — an
  HMAC-SHA256 of the **raw body** keyed with the app secret, compared in
  constant time. Missing, malformed or wrong signatures → `401`. The raw body
  is hashed before parsing (no parse-then-sign bypass).
- **Payload size**: bodies over `QBIT_WEBHOOK_MAX_BODY_BYTES` → `413`.
- **Replay/stale protection**: events with a provider timestamp older than
  `QBIT_WEBHOOK_MAX_AGE_SECONDS` are stored for forensics but never applied —
  a replay cannot move state.
- **No secrets in logs**: signatures, tokens and authorization headers are
  never logged; logs carry request_id, provider, event ids and counts only.

## Processing pipeline (§17, §19)

```
POST (raw body)
  → signature validation
  → parse entry[].changes[].value
  → resolve sending account by value.metadata.phone_number_id (multi-account)
  → WhatsAppEventNormalizer
      statuses[] → DELIVERY events (sent/delivered/read/failed/deleted)
      messages[] → INBOUND events (text; other types stored without bodies)
  → ProviderEvent row  (UNIQUE provider+provider_event_id → idempotent, §20)
  → DELIVERY: match CampaignRecipient by provider_message_id
              → forward-only state machine (§21)
              → CampaignEvent (append-only) → analytics
  → INBOUND:  lead match → Conversation → Message (§22–§24)
  → 200 {"received": n, "duplicates": n, "applied": n, "inbound": n, "stale": n}
```

## Idempotency (§20)

Provider events can be delivered more than once. `provider_events` has a
UNIQUE constraint on `(provider, provider_event_id)` where the event id is the
composite `"<wamid>:<status>"` for delivery receipts and `"<wamid>:inbound"`
for messages. A redelivered webhook is a stored-once no-op, so sent/delivered/
read/failed counters can never double-count.

## Event state machine (§21)

```
PENDING → QUEUED → SENDING → SENT → DELIVERED → READ → REPLIED
                          ↘ FAILED (from QUEUED/SENDING/SENT only)
```

- transitions are forward-only: `READ → QUEUED` is impossible regardless of
  event arrival order; out-of-order events are absorbed (delivered before sent
  simply moves the status forward)
- `FAILED` is terminal
- timestamps (`sent_at`, `delivered_at`, `read_at`, `replied_at`) fill on
  first occurrence and are never overwritten
- webhook code never manipulates UI state directly — the UI reads the same
  rows through the normal API/analytics path

## Inbound messages & Inbox foundation (§22–§24)

Phase 6 establishes the backend event model; the Inbox UI is a later phase.

```
Webhook → normalized message → lead match → Conversation → Message
```

- **Matching**: `sending_account` + normalized phone. When a real Lead matches,
  the conversation attaches to it; otherwise the conversation stays lead-less
  (`PENDING`, an unresolved contact). Personal information is never invented
  and duplicate leads are never silently created (§24).
- **Replies**: when the sender matches a campaign recipient currently in
  SENT/DELIVERED/READ, the recipient moves to REPLIED and a `MESSAGE_REPLIED`
  campaign event is recorded (feeds campaign Replies counts). Replies that
  cannot be tied to a campaign are stored in the conversation only — nothing
  is fabricated.
- Message rows carry `direction` (INBOUND/OUTBOUND), `provider_message_id`,
  `message_type` (TEXT, IMAGE, …), `body` (text messages only), `status`,
  sanitized `metadata`. No provider secrets ever appear in metadata.

## Testing (§41/§42)

The automated suite covers: verification challenge (accept/wrong token/wrong
mode/unconfigured), forged/missing/invalid signatures, duplicate and replayed
events, sent/delivered/read/failed flows, backward-transition rejection,
inbound text + attachment types, lead matching, unresolved contacts, campaign
reply linkage, and secret-redaction in logs — all against the real endpoints
with HMAC-signed payloads.
