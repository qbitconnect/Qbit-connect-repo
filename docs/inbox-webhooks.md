# Inbox Webhooks (Phase 8 §38–§41)

Both channels deliver events over signature-verified, idempotent webhooks.
These routes are intentionally NOT JWT-authenticated — they are machine
endpoints secured exactly as each provider contract requires. Nothing
secret-like (signatures, tokens, authorization headers) is ever logged.

## WhatsApp (existing contract, Phase 6 — now inbox-aware)

```
GET  /api/v1/webhooks/whatsapp   Meta subscription handshake (hub.verify_token)
POST /api/v1/webhooks/whatsapp   X-Hub-Signature-256 (HMAC-SHA256 of RAW body,
                                 app secret) → size cap → stale-event window →
                                 normalize → ProviderEvent (idempotent) →
                                 campaign recipients → Message rows → unread
```

Delivery statuses (`sent`/`delivered`/`read`/`failed`) now ALSO mirror onto
inbox Message rows, forward-only (§41). Inbound messages create/extend
conversations, match leads, count unread and can reopen resolved threads.
Duplicate deliveries are stored once and applied once.

## Email inbound (new in Phase 8)

```
POST /api/v1/webhooks/email/inbound/{provider}
```

Contract — the SAME mechanism as the Phase 7 delivery webhook:

```
X-QBIT-Signature:  sha256=<hex hmac-sha256(raw body, shared secret)>
X-QBIT-Timestamp:  <unix seconds, ±QBIT_WEBHOOK_MAX_AGE_SECONDS>
```

Secret: `EMAIL_WEBHOOK_SECRET` (the mock provider also accepts the test-only
default in test environments). Payload — single message or `{"messages": []}`:

```json
{
  "message_id":  "<abc@mail.provider>",       // required — idempotency id
  "from":        "Ravi Patel <ravi@acme.test>",  // required; Name <addr> parsed
  "to":          "support@yourdomain.com",    // resolves the sending account
  "subject":     "Re: Quote #123",
  "text":        "Looks good, proceed.",
  "html":        "<p>…</p>",                  // optional; sanitized at display
  "in_reply_to": "<quote-1@yourdomain>",
  "references":  "<quote-0@…> <quote-1@…>",
  "occurred_at": "2026-09-07T10:00:00Z"       // optional ISO or epoch
}
```

Pipeline: signature + replay validation → RFC-5322 address extraction →
`ProviderEvent` (UNIQUE `email_inbound:<provider>` + `<message_id>:inbound`)
→ thread match by (account, normalized sender email) → lead match →
Message (threading headers stored) → unread → campaign REPLIED linkage.

Why a webhook (not IMAP): the platform standardizes on provider-style signed
webhook events; a poller would add stored mailbox credentials and state
beyond this phase. The ingestion service behind the route is transport-
agnostic — a mailbox poller can feed the same `EmailInboundService` later
without any schema change.

## Idempotency & ordering guarantees (§40, §41)

- every event is stored exactly once (`ProviderEvent` unique gate)
- every inbound message is stored exactly once per conversation
- out-of-order READ/DELIVERED can never downgrade a message state
- late (stale) events are stored but not applied — replay protection without
  data loss
- processing is transactional: an application failure never corrupts stored
  raw events

## Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| 401 on POST | missing/invalid signature or stale timestamp — verify secret and clock |
| 413 | payload over `QBIT_WEBHOOK_MAX_BODY_BYTES` |
| `stored: 0, invalid: n` | missing `message_id`/`from`, or unparseable sender address |
| `duplicates: n` | redelivery of a known event id — expected and safe |
| message stored but campaign not REPLIED | no matching SENT recipient for that thread — linkage is honest, never guessed |
| conversation has no unread growth | duplicate event or stale (older than the replay window) |
