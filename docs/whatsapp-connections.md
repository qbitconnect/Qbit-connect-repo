# WhatsApp Connections (Phase 6 §5, §6, §31, §32, §35)

Everything about connecting and operating WhatsApp Business sending accounts.
UI: `/connections` → WhatsApp section. API: `/api/v1/connections/...`.

## Multi-account architecture (§3)

QBIT Connect is built for MANY sending accounts — there is no global WhatsApp
number anywhere in the schema or code. Each `sending_accounts` row is one
independent WhatsApp Business number with its own credentials, health, status,
capabilities and template catalog:

```
Campaign ──► SendingAccount (QBIT WhatsApp 01) ──► provider calls
Campaign ──► SendingAccount (QBIT WhatsApp 02) ──► provider calls
Campaign ──► SendingAccount (QBIT WhatsApp 20) ──► provider calls
```

Phase 6 rule: **one campaign → one selected sending account** (no random
selection, no hidden pooling). The account pool pattern is future work.

Account fields: `id, name, channel=WHATSAPP, provider, phone_number_id,
business_account_id, status, health_status, capabilities, metadata(config),
credential_ref, created_at, updated_at, last_health_check`.

## Connection statuses (§6)

| Status | Meaning |
|---|---|
| `PENDING` | created, provider validation not yet passed |
| `ACTIVE` | provider validated every step |
| `INACTIVE` | manually disabled by an operator |
| `ERROR` | validation failed / provider rejected configuration |
| `DISCONNECTED` | provider access lost |
| `SUSPENDED` | provider-side restriction |

Health: `HEALTHY / DEGRADED / UNHEALTHY / UNKNOWN` (from the quality probe).

## Add-account wizard (§32)

`/connections/whatsapp/new` — one form, seven visual steps:

1. Account name
2. Provider (WhatsApp Business Cloud API; `whatsapp_mock` visible in test envs only)
3. Credentials (access token [+ optional per-account app secret]) — write-only password fields
4. Validate — token → phone number → business account → permissions
5. Business/phone details — `phone_number_id`, `business_account_id` (WABA)
6. Health check — quality probe
7. Complete — account page shows the real connection state

The wizard runs validation + health check on submit. **An account only becomes
ACTIVE when the provider confirms every validation step** — otherwise it stays
ERROR with the exact provider message (sanitized) on the account page.

## Connection validation steps (§5)

`POST /api/v1/connections/whatsapp/{id}/validate` returns:

```json
{
  "ok": true,
  "checked_at": "...",
  "steps": {
    "credentials":            {"ok": true, "detail": "access token present"},
    "phone_number_configured":{"ok": true, "detail": "111222333"},
    "phone_number_valid":     {"ok": true, "detail": {"display_phone_number_masked": "+49••••••678",
                                                       "verified_name": "...", "quality_rating": "GREEN"}},
    "permissions":            {"ok": true, "detail": "phone number readable with this token"},
    "business_account":       {"ok": true, "detail": {"name": "...", "business_verification_status": "APPROVED"}}
  }
}
```

A failed step means status → ERROR; the UI shows the failing step verbatim.

## Secret management (§4)

- Credentials are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256).
  The key is derived from `QBIT_SECRET_KEY` via HKDF-SHA256 with a dedicated
  salt/info — no additional secret infrastructure is required (self-hosted
  friendly, Phase 2-compatible).
- Ciphertext lives in the `provider_credentials` table; `sending_accounts`
  references it by `credential_ref` (a name, never a secret).
- Decrypted credentials exist only in memory for the duration of one provider
  call. They are never returned by any API, never logged, never cached.
- API responses show `has_credentials: true` and token **tails** only
  (`••••••1234` style); phone numbers are masked (`+49••••••678`).
- Rotation: `PATCH /connections/whatsapp/{id}` with a `credentials` object
  (or the UI credential form). Rotation resets the validated state — run
  Validate again afterwards.
- If `QBIT_SECRET_KEY` changes, existing ciphertexts fail loudly with a clear
  re-enter-your-credentials error (never silent corruption).

## Permissions (§37)

| Action | Permission |
|---|---|
| view connections | `connections.view` |
| create | `connections.create` |
| edit / disable / rotate credentials | `connections.edit` |
| remove | `connections.delete` |
| validate | `connections.validate` |
| health check | `connections.health` |
| sync templates | `connections.sync_templates` |

Every check is enforced server-side on both API and UI routes.

## API surface (§36)

```
GET    /api/v1/connections
GET    /api/v1/connections/whatsapp
POST   /api/v1/connections/whatsapp
GET    /api/v1/connections/whatsapp/{id}
PATCH  /api/v1/connections/whatsapp/{id}
DELETE /api/v1/connections/whatsapp/{id}
POST   /api/v1/connections/whatsapp/{id}/validate
POST   /api/v1/connections/whatsapp/{id}/health
POST   /api/v1/connections/whatsapp/{id}/sync-templates
GET    /api/v1/connections/whatsapp/{id}/templates
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| validate → `AUTHENTICATION_ERROR` | token expired/insufficient | rotate credentials, re-validate |
| validate → `PERMISSION_ERROR` | app lacks WABA scopes | grant `whatsapp_business_messaging` + `whatsapp_business_management` |
| health UNHEALTHY, quality RED | provider flagged the number | see provider panel; never send through a restricted number |
| sync-templates → `BUSINESS_ACCOUNT_MISSING` | no WABA id on the account | set Business Account ID in account details |
| account cannot be set ACTIVE | provider validation not passed | run Validate; only the provider's acceptance activates an account |
