# WhatsApp Business Provider Integration (Phase 6)

QBIT Connect integrates with WhatsApp through the **official WhatsApp
Business Cloud API** (Graph API) only. This document covers the provider
adapter; account setup lives in [whatsapp-connections.md](whatsapp-connections.md),
templates in [whatsapp-templates.md](whatsapp-templates.md), and webhook
delivery in [whatsapp-webhooks.md](whatsapp-webhooks.md).

## Compliance boundary (non-negotiable)

The integration uses only provider-supported APIs. There is **no** WhatsApp
Web automation, no QR-session scraping, no personal-number automation, no
stealth/anti-ban/bypass logic, and none will be added. When the provider
rejects an operation (rate limit, template not approved, messaging window,
policy restriction), QBIT Connect surfaces the real, sanitized error to the
operator — it never attempts to evade a restriction. Rate limiting in QBIT
Connect is operational throttling only (a pacing tool), never an evasion tool.

## Architecture

```
CampaignService (provider-agnostic)
        ↓ resolves via registry
WhatsAppProvider  (providers/whatsapp/provider.py, provider_id = whatsapp_cloud)
        ↓ uses
WhatsAppCloudClient  (providers/whatsapp/client.py, official Graph API)
        ↓
WhatsApp Business Platform
```

- `providers/whatsapp/client.py` — thin async httpx wrapper over:
  - `GET /{phone_number_id}` — health probe + connection validation
  - `GET /{business_account_id}` — business account validation
  - `GET /{business_account_id}/message_templates` — template catalog
  - `POST /{phone_number_id}/messages` — template message send
- `providers/whatsapp/errors.py` — `WhatsAppErrorNormalizer`
- `providers/whatsapp/provider.py` — the `WhatsAppProvider` adapter
- `providers/whatsapp/mock.py` — `whatsapp_mock`, TEST-ONLY simulator (§41)

`CampaignService` never imports the adapter; it resolves whatever provider the
SendingAccount references through the registry, so the Email provider (Phase 7)
will slot in unchanged.

## Send flow (§14)

1. Worker claims a queue item (idempotency key `campaign:recipient:version`).
2. Credentials are resolved from the encrypted vault for this call only.
3. The provider template payload is rendered per recipient (`{{1}}, {{2}}`
   placeholders ← lead fields mapped in `template.variables`, in order).
4. A recipient whose lead lacks a required variable value is SKIPPED with
   `MISSING_TEMPLATE_VARIABLE` — incomplete templates are never sent.
5. `POST /{phone_number_id}/messages` → normalized `SendResult`
   (`provider_message_id` = wamid, status SENT on `accepted`).
6. Errors are normalized (see below); TRANSIENT errors retry with backoff,
   PERMANENT errors fail immediately.

## Error normalization (§16)

| Canonical code | Provider signals | Class |
|---|---|---|
| `RATE_LIMITED` | 4, 47, 80007, 130429, 131048, HTTP 429 | TRANSIENT (respects retry-after) |
| `PROVIDER_UNAVAILABLE` | network/timeout, HTTP 5xx | TRANSIENT |
| `AUTHENTICATION_ERROR` | 190, 102, HTTP 401, "access token"/OAuth text | PERMANENT |
| `PERMISSION_ERROR` | 10, 200, HTTP 403 | PERMANENT |
| `INVALID_RECIPIENT` | 131026, 131030 | PERMANENT |
| `MESSAGE_REJECTED` | 131047, 131049 (re-engagement/policy) | PERMANENT |
| `INVALID_TEMPLATE` | 132000–132999 family | PERMANENT |
| `TEMPLATE_NOT_APPROVED` | 132xxx + "not approved/paused/pending" text | PERMANENT |
| `ACCOUNT_ERROR` | 133010, 131031, 131005, 368 | PERMANENT |
| `UNKNOWN_PROVIDER_ERROR` | anything else | by HTTP family |

Only TRANSIENT errors are retried, and only up to `QBIT_MARKETING_MAX_ATTEMPTS`.
When the provider supplies a retry-after hint, the backoff never fires earlier
than requested (§30 — we respect the hint; we do not use it to time evasion).

## Health check (§7)

`POST /api/v1/connections/whatsapp/{id}/health` probes the provider for
credentials validity, availability, phone-number configuration and quality:

| Graph `quality_rating` | QBIT health |
|---|---|
| GREEN | HEALTHY |
| YELLOW / unknown | DEGRADED |
| RED | UNHEALTHY |
| token rejected / unreachable | UNHEALTHY |

Stored on the account: `health_status`, `last_health_check`, and a sanitized
`last_health_error` summary. Raw provider payloads are never stored or shown.

## Configuration (§2)

Environment variables (see `.env.example`):

- `WHATSAPP_PROVIDER` — default registry id for new connections (`whatsapp_cloud`)
- `WHATSAPP_API_BASE_URL` — default `https://graph.facebook.com`
- `WHATSAPP_API_VERSION` — default `v21.0`
- `WHATSAPP_WEBHOOK_VERIFY_TOKEN` — required in production
- `WHATSAPP_APP_SECRET` — app-level webhook signature (per-account vault secret takes precedence)
- `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_BUSINESS_ACCOUNT_ID` / `WHATSAPP_PHONE_NUMBER_ID` — single-account bootstrap fallbacks

Secrets are never hard-coded, never committed, never returned by an API, never
printed in logs. Per-account credentials live in the encrypted vault — see
[whatsapp-connections.md](whatsapp-connections.md) § Secret management.
