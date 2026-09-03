# Email Provider Architecture (Phase 7)

## Overview

Phase 7 connects the QBIT Connect Marketing Engine to REAL, provider-supported
email delivery. The campaign engine contains ZERO vendor code — every channel
difference lives behind the provider abstraction:

```
BaseMarketingProvider (app/services/marketing/providers/base.py)
        |
        +-- SMTPProvider              official RFC 5321 SMTP (aiosmtplib), TLS/STARTTLS
        +-- GenericEmailAPIProvider   vendor-neutral HTTP API contract
        +-- WhatsAppProvider          official WhatsApp Cloud API (Phase 6 foundation)
        +-- MockEmailProvider         MOCK / TEST ONLY — refused in production
        +-- MockWhatsAppProvider      MOCK / TEST ONLY — refused in production
```

## Contract

Every provider implements:

| Method | Purpose |
|---|---|
| `validate_configuration(config, credentials)` | static configuration check |
| `validate_sender(config, credentials, sender)` | from/reply-to/phone-id validation |
| `validate_recipient(recipient)` | per-recipient address validation |
| `validate_message(message)` | body/subject/headers sanity (CRLF rejection) |
| `send(config, credentials, message)` | one recipient per call (privacy-safe) |
| `get_status(config, credentials, id)` | provider message status (where supported) |
| `handle_event(payload, headers, creds)` | webhook payload → `NormalizedEvent` list |
| `health_check(config, credentials)` | real connectivity + auth round-trip |

## Registry

`registry.get_provider(channel, provider_id, is_production=…)` resolves the
adapter. Mock providers (`mock_email`, `mock_whatsapp`) raise
`ValidationError` whenever `is_production=True` — enforced BOTH at the
account-creation layer (SendingAccountService) and at send time.

## Error normalization (§22–§23)

`providers/errors.py` maps every failure to a category + retry class:

| Category (email) | Class |
|---|---|
| INVALID_RECIPIENT, AUTHENTICATION_ERROR, TLS_ERROR, MESSAGE_REJECTED, CONFIGURATION_ERROR | PERMANENT — never retried |
| CONNECTION_ERROR, RATE_LIMITED, MAILBOX_UNAVAILABLE, PROVIDER_UNAVAILABLE | TRANSIENT — exponential backoff while attempts remain |
| UNKNOWN | small retry budget, then permanent |

RATE_LIMITED honoring is **compliance** with provider guidance — never bypassed.

## Non-goals (§64)

No spam-filter manipulation, reputation evasion, IP rotation for restriction
evasion, rate-limit bypass, or unauthorized address harvesting. Only official
transports are implemented.
