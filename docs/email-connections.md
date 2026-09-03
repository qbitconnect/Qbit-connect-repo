# Email Connections — Sending Accounts (Phase 7 §2, §4–§9)

## Adding an email sender account

UI: `/connections` → **Add Email account** (7-step wizard: name → provider →
sender details → configuration → validate → health check → complete).

API:

```
POST /api/v1/connections/email
{
  "name": "QBIT Marketing",
  "provider": "smtp",            // smtp | email_api
  "sender_name": "QBIT Team",
  "sender_email": "marketing@yourcompany.com",
  "reply_to": "replies@yourcompany.com",
  "config":  {"host": "smtp.yourcompany.com", "port": 587, "security": "STARTTLS"},
  "credentials": {"username": "…", "password": "…"}   // encrypted before storage
}
```

### SMTP configuration

| Key | Values |
|---|---|
| `host` | required |
| `port` | 1–65535 (587 STARTTLS / 465 TLS typical) |
| `security` | `TLS` (implicit) or `STARTTLS` — nothing else is accepted |
| credentials | `username`, `password` (vault) |

### Email API configuration

| Key | Values |
|---|---|
| `api_base_url` | https base of the provider API |
| credentials | `api_key` (Authorization: Bearer …) |
| contract | `POST {base}/messages` → 2xx `{"message_id": "…"}` |

## Multi-account

There is NO global sender. Create as many accounts as you need (QBIT Sales,
QBIT Marketing, QBIT Support…). Each campaign selects exactly one account;
each account keeps its own credentials, capabilities and health state.

## Validation & states (§7)

`POST /api/v1/connections/email/{id}/validate` runs configuration → sender →
live connectivity checks. Only a full success sets `ACTIVE` + `HEALTHY`;
failures keep `PENDING/ERROR` with the sanitized provider reason. Any
credential/config change demotes ACTIVE back to PENDING (re-validate).

Account status: `PENDING ACTIVE INACTIVE ERROR DISCONNECTED SUSPENDED`
Health: `HEALTHY DEGRADED UNHEALTHY UNKNOWN`

Campaigns refuse to launch on accounts that are not ACTIVE or whose health is
UNHEALTHY — the API returns `SENDING_ACCOUNT_UNHEALTHY` with the reason (§41).

## Secret safety (§6)

- credentials are Fernet-encrypted at rest (HKDF-derived from QBIT_SECRET_KEY)
- never returned by any API response (`to_public_dict` masks the vault ref)
- never logged (audit redaction + structured logging filters)
- rotating the platform secret invalidates stored ciphertexts by design —
  re-enter credentials
