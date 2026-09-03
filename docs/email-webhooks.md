# Email Webhooks (Phase 7)

Providers report delivery outcomes to:

```
POST /api/v1/webhooks/email/{provider}        provider ∈ {email_api, email_mock, smtp}
```

The route is intentionally **not JWT-authenticated** — it is called by the provider and secured by the webhook contract below.

## Authentication (§28)

Every POST carries two headers:

```
X-QBIT-Signature:  sha256=<hex hmac-sha256(RAW body, shared secret)>
X-QBIT-Timestamp:  <unix seconds>
```

- the HMAC is computed over the **raw body before parsing** (parsed-then-signed is a classic bypass)
- constant-time comparison; missing/invalid → `401`
- timestamps outside `QBIT_WEBHOOK_MAX_AGE_SECONDS` (default 600s) → `401` (replay protection)
- payloads above `QBIT_WEBHOOK_MAX_BODY_BYTES` → `413`
- the shared secret comes from `EMAIL_WEBHOOK_SECRET`; the mock provider accepts a test-only default in test environments
- **never** logged: signatures, secrets, authorization headers (§57)

These are the mechanics of the QBIT email contract — the platform never invents verification logic for third-party vendors; a future vendor adapter maps *its* documented mechanism onto this pipeline.

## Event envelope

Single event or a batch:

```json
{
  "events": [
    {
      "provider_event_id": "vendor-evt-42",
      "message_id": "<abc@company.com>",
      "event": "delivered",
      "timestamp": 1756000000,
      "bounce": {"type": "hard", "diagnostic": "user unknown"},
      "recipient": "someone@example.com"
    }
  ]
}
```

Events: `sent`, `delivered`, `bounced` (+`bounce.type: hard|soft`), `complaint`, `unsubscribed`, `failed`, `opened`, `clicked`. Unknown events are rejected honestly (stored raw, never guessed into shape).

## Processing pipeline (§27)

```
webhook → signature+timestamp validation → parse → EmailEventNormalizer
        → ProviderEvent store (UNIQUE provider, provider_event_id)
        → idempotency check → update recipient (forward-only state machine)
        → suppression (hard bounce / complaint / unsubscribe)
        → CampaignEvent → analytics
```

## Idempotency (§26)

`ProviderEvent` rows are `UNIQUE (provider, provider_event_id)`. A redelivered webhook is a no-op — delivered/bounce/complaint counters can never double-count. When the provider does not supply an event id, a stable composite (`<message_id>:<event>`) is synthesized.

## Response

```json
{"success": true, "data": {"received": 2, "duplicates": 1, "applied": 1, "stale": 0, "unmatched": 0, "suppressed": 0}}
```

- `stale` — older than the replay window: stored, never applied
- `unmatched` — no campaign recipient carries that `message_id` (e.g. a manual message): the raw event is kept, nothing is fabricated

## Testing your integration

The mock smoke path (`scripts/phase7_smoke.py`) and the test-suite (`tests/marketing/test_email_delivery.py`) exercise signed delivery, duplicates, hard/soft bounce, complaint and unsubscribe flows end to end.
