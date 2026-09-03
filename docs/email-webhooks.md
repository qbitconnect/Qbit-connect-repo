# Email Webhooks (Phase 7 §27, §28)

## Endpoints

```
POST /api/v1/webhooks/email/{provider}     # provider in: smtp | email_api | mock_email(test)
GET  /api/v1/webhooks/whatsapp             # Cloud API verification challenge
POST /api/v1/webhooks/whatsapp             # Cloud API events
```

These routes are PUBLIC by design (providers cannot authenticate as users) —
every payload is verified before anything is applied.

## Generic email verification (§28)

Two headers, both required:

| Header | Meaning |
|---|---|
| `X-QBIT-Signature` | `sha256=<hex HMAC-SHA256(raw body, shared secret)>` |
| `X-QBIT-Timestamp` | unix seconds; rejected outside tolerance (replay protection) |

Configure the shared secret via `QBIT_MARKETING_WEBHOOK_SECRET`. Invalid
signature → 401. Stale timestamp (> `QBIT_MARKETING_WEBHOOK_TIMESTAMP_TOLERANCE`
seconds, default 300) → 401. Bodies > 1 MB are rejected.

WhatsApp Cloud API uses the OFFICIAL `X-Hub-Signature-256` scheme
(`QBIT_MARKETING_WHATSAPP_APP_SECRET`) and the GET challenge flow
(`QBIT_MARKETING_WHATSAPP_VERIFY_TOKEN`). Nothing is invented.

## Payload (generic contract)

```json
{"events": [
  {"id": "evt-1",            // provider event id — REQUIRED (idempotency)
   "type": "delivered",      // send|delivered|bounce|complaint|open|click|reply|fail
   "message_id": "…",        // matches the stored provider_message_id
   "recipient": "user@x.com",// optional fallback matcher
   "hard": true,             // bounce hardness
   "reason": "mailbox unavailable",
   "meta": {"…": "…"}}
]}
```

Vendor-specific schemas are translated by subclassing `EmailEventNormalizer`.

## Idempotency & application (§26, §27)

`(channel, provider, provider_event_id)` is UNIQUE. A duplicate webhook is
stored as `ProviderEvent(status=DUPLICATE)` and never re-applied — analytics
cannot double-count. Application path:

```
verify → parse → normalize → idempotency gate → recipient match
      → forward-only state transition → CampaignEvent → counters
      → side effects (suppression on hard bounce / complaint / unsubscribe)
```

Unmatched events (unknown `message_id`) are recorded and skipped honestly —
nothing is guessed.

## Security rules

- authorization headers and webhook secrets are NEVER logged (§57)
- forged / replayed / unsigned requests are rejected with 401
- payloads are size-capped; JSON parsing failures are rejected
