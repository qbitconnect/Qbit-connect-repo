# Email Provider (Phase 7)

The EMAIL channel connects to **real, provider-supported email delivery** through the same provider abstraction WhatsApp uses. Campaign logic never talks to SMTP or an email API directly — `CampaignService` resolves whichever provider the sending account references.

## Architecture

```
BaseMarketingProvider
    +-- WhatsAppProvider           (Phase 6 — WhatsApp Business Cloud API)
    +-- SMTPProvider               (Phase 7 — provider id: smtp)
    +-- GenericEmailAPIProvider    (Phase 7 — provider id: email_api)
    +-- EmailMockProvider          (MOCK / TEST ONLY — id: email_mock)
    +-- SMSProvider                (interface — later phase)
```

Provider responsibilities (and nothing else):

| Method | Purpose |
|---|---|
| `validate_configuration` | structural config check (never echoes secrets) |
| `validate_sender` / account validation | the §7 connection flow (config → sender → connectivity → auth) |
| `validate_recipient` | email format validation |
| `validate_message` | subject required, size limits, CRLF guard |
| `send` | one message → `SendResult` (no secrets in result/logs) |
| `handle_event` | webhook/event normalization |
| `health_check` | connect(+auth)+QUIT probe — never sends mail |

## SMTP adapter

- transport security: `TLS` (implicit, port 465), `STARTTLS` (587) or `NONE` (25 — not recommended)
- per-call credentials from the encrypted vault (`smtp_username`, `smtp_password`)
- individual recipient delivery only — one `To:` per message, never CC/BCC
- generates a standards-compliant `Message-ID`, which becomes `provider_message_id`
- timeout AFTER the data phase is classified `DELIVERY_STATE_UNKNOWN` and is **never retried** (duplicate-email protection, §20)

## Generic Email API adapter

A vendor-neutral HTTP contract (see `docs/email-webhooks.md` for the event side):

```
POST {EMAIL_API_BASE_URL}/messages
Authorization: Bearer {api_key}
Idempotency-Key: {campaign:recipient:version}
{
  "from": {"email": "sender@company.com", "name": "QBIT Sales"},
  "to": [{"email": "recipient@example.com"}],
  "reply_to": "reply@company.com",
  "subject": "...", "text": "...", "html": "...",
  "headers": {"Message-ID": "<...>"}
}
→ 2xx {"id": "<provider_message_id>"}
```

A future vendor-specific adapter plugs into the same `BaseMarketingProvider` shape without touching campaign code.

## Error normalization (§23)

| Code | Class | Typical cause |
|---|---|---|
| `INVALID_RECIPIENT` | PERMANENT | bad mailbox / recipient rejected |
| `AUTHENTICATION_ERROR` | PERMANENT | wrong username/password/API key |
| `CONNECTION_ERROR` | TRANSIENT | refused / dropped connection |
| `TLS_ERROR` | PERMANENT | certificate / TLS negotiation failure |
| `RATE_LIMITED` | TRANSIENT | 4xx/429 — honors `retry_after` |
| `MAILBOX_UNAVAILABLE` | TRANSIENT (4xx) / PERMANENT (5xx) | full mailbox / user unknown |
| `MESSAGE_REJECTED` | PERMANENT | provider refused content |
| `PROVIDER_UNAVAILABLE` | TRANSIENT | DNS / 5xx / upstream down |
| `CONFIGURATION_ERROR` | CONFIGURATION | misconfigured account |
| `DELIVERY_STATE_UNKNOWN` | PERMANENT | timeout after data phase — never auto-retried |

Only TRANSIENT failures retry, with exponential backoff capped by `QBIT_MARKETING_RETRY_MAX_SECONDS`; `retry_after` hints are honored (that is compliance, not evasion).

## Compliance notes

- official/provider-supported transports only (SMTP with TLS or the provider's API)
- no spam-filter bypass, reputation manipulation, IP rotation for evasion, or rate-limit evasion — anywhere
- provider restrictions are surfaced honestly and never bypassed
