# Email Connections (Phase 7)

`/connections/email` manages **email sender accounts** — one row per sender identity (e.g. `sales@company.com`, `marketing@company.com`, `support@company.com`). Multiple sender accounts are supported; there is **no global single sender**.

## Fields

| Field | Where | Notes |
|---|---|---|
| name | column | display name ("QBIT Marketing") |
| channel | column | always `EMAIL` |
| provider | column | `smtp` / `email_api` / (`email_mock` in tests only) |
| sender_name | config_metadata | "From" display name |
| sender_email | identifier | the From address (also the matching key for conversations) |
| reply_to | config_metadata | optional Reply-To header |
| status | column | PENDING → ACTIVE / ERROR / DISCONNECTED / SUSPENDED / INACTIVE |
| health_status | column | HEALTHY / DEGRADED / UNHEALTHY / UNKNOWN |
| credential_ref | column | pointer into the encrypted vault (never the secret itself) |
| last_health_check | column | UTC timestamp of the last probe |

## Add-account wizard (§9)

1. **Account name**
2. **Provider type** — SMTP or Email API
3. **Sender details** — sender name / sender email / reply-to
4. **Provider configuration** — host/port/security or API base/region
5. **Validate** — configuration → sender → connectivity → authentication
6. **Health check** — connect(+auth)+QUIT; no email is ever sent
7. **Complete** — ACTIVE only when every step passed

If any validation step fails the account is marked **ERROR** and the **real provider error is shown** (sanitized). An account is never falsely activated.

## Status rules

- `ACTIVE` requires a successful validation (`config_metadata.configured == true`)
- rotating credentials resets `configured` and demotes ACTIVE → INACTIVE until re-validation
- an UNHEALTHY account never receives queued sends — launch is blocked with `EMAIL_SENDER_UNHEALTHY`
- removing an account also removes its encrypted credential row

## Secret handling (§6)

- SMTP password / API key are **write-only**: accepted on create/rotate, Fernet-encrypted at rest (HKDF-derived key from `QBIT_SECRET_KEY`), never returned by any API, never logged, never displayed in the UI
- display hints are tail-only (`••••abcd`)
- RBAC: `email.connections.view/create/edit/delete/validate/health`

## API (§45)

```
GET    /api/v1/connections/email
POST   /api/v1/connections/email
GET    /api/v1/connections/email/{id}
PATCH  /api/v1/connections/email/{id}
DELETE /api/v1/connections/email/{id}
POST   /api/v1/connections/email/{id}/validate
POST   /api/v1/connections/email/{id}/health
GET    /api/v1/connections/email/{id}/templates
GET    /api/v1/connections/email/{id}/reputation
```

## Sender reputation (§43)

`GET .../reputation` reports per-account delivery/bounce/complaint metrics computed from **actual campaign events**, with threshold warnings (`QBIT_EMAIL_BOUNCE_WARN_RATE`, `QBIT_EMAIL_COMPLAINT_WARN_RATE`). This is a monitoring foundation only — it never guarantees inbox placement and no spam-filter analysis exists in the platform.
