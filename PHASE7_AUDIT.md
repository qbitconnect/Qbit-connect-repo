# PHASE 7 AUDIT — Email Marketing Provider Integration

**Date:** 2026-09-04 · **Base:** `main @ a764d3c` (Phase 6 complete) · **Branch state:** Phase 1–6 implemented and verified (PHASE5_AUDIT.md, PHASE6_AUDIT.md present).

STEP 0 audit of the existing repository before any Phase 7 coding, per the Phase 7 brief.

---

## 1. Reusable provider abstraction

| Component | Location | Status |
|---|---|---|
| `BaseMarketingProvider` | `app/services/marketing/providers/base.py` | REUSE — full contract: `validate_configuration / validate_recipient / validate_message / send / validate_send_requirements / get_status / handle_event / health_check`, `SendResult`, `ErrorClass` (TRANSIENT/PERMANENT/CONFIGURATION), `ProviderNotConfigured` |
| `EmailProvider` | `app/services/marketing/providers/interfaces.py` | REPLACE — honest interface stub (`interface_only=True`, `send()` returns `PROVIDER_NOT_IMPLEMENTED`). Phase 7 replaces the registry entry with real SMTP + Email-API adapters |
| Provider registry | `app/services/marketing/providers/__init__.py` | REUSE/EXTEND — `build_provider_registry(settings)`; mock providers gated to non-production |
| WhatsApp reference pattern | `app/services/marketing/providers/whatsapp/*` | REUSE (pattern only) — client/error-normalizer/provider/mock split, transport-factory test hook, sanitized health reports |
| Mock provider gating | `providers/mock.py`, `providers/whatsapp/mock.py` | REUSE — `test_only=True`, registered only when `QBIT_ENV == "test"` or explicit dev flag |

## 2. Reusable campaign queue

`app/services/marketing/queue.py` — DB-backed `campaign_queue` with
`UNIQUE (campaign_id, recipient_id, message_version)` idempotency, guarded-UPDATE
lease claiming, exponential backoff (`QBIT_MARKETING_RETRY_BASE/MAX_SECONDS`),
`provider_retry_after` respect, per-account minute/hour rate gate
(`config_metadata.rate_policy`), pause/cancel semantics. **No email-specific changes
required** except accepting `emails_per_minute/emails_per_hour` as aliases for the
rate-policy keys (Phase 7 §42 vocabulary).

## 3. Reusable template system

`app/services/marketing/template.py` + `CampaignTemplate` model:
`{{variable}}`-only substitution (no expression language, never executed),
malformed-bracket rejection, allowlist-driven (`RENDER_VARIABLES`), subject
required for EMAIL already (channel spec). **Missing for email** (added in Phase 7):
HTML body semantics, plain-text fallback, HTML sanitization, header-injection
guards, `unsubscribe_url` / `email` / `phone` / `website` / `company_*` variables.

## 4. Reusable suppression

`app/services/marketing/suppression.py` — `SuppressionEntry` (EMAIL/PHONE/LEAD/CHANNEL
+ reason), `OptOutRecord` (append-once evidence, removal refused while opt-out
exists), batched `check_batch`, normalized addresses via
`app/services/scraping/lead_keys.normalize_email`. **EMAIL channel works today.**
Opt-out records always create the matching suppression entry. Phase 7 reuses this
for unsubscribe + bounce/complaint suppression — no duplicate system (brief §47).

## 5. Reusable event architecture

- `CampaignEvent` — append-only, `provider_event_id` column present.
- `ProviderEvent` (`app/models/messaging.py`) — `UNIQUE (provider, provider_event_id)`
  idempotency gate, already provider-agnostic → **reused directly for email
  webhooks** (`provider = "email_api" / "email_mock"`).
- `app/services/marketing/state.py` — forward-only recipient state machine
  (PENDING→QUEUED→SENDING→SENT→DELIVERED→READ→REPLIED, FAILED terminal).
- `EventService` — structured campaign events.

## 6. Missing email functionality (implemented in Phase 7)

1. Real `SMTPProvider` + `GenericEmailAPIProvider` adapters (send, validation, health).
2. `EmailErrorNormalizer` — SMTP/API error → canonical code + TRANSIENT/PERMANENT.
3. Email normalization service (trim, lowercase domain, format validation, `email_normalized`).
4. Unsubscribe architecture: secure tokens (hash-at-rest), public `GET /unsubscribe/{token}`,
   real opt-out → suppression → future sends SKIPPED.
5. HTML sanitization, CRLF/header-injection guards, recipient privacy (1:1 sends).
6. Open/click tracking (campaign-level opt-in, signed redirect, no raw DB IDs in URLs).
7. Bounce/complaint handling with hard/soft classification + suppression.
8. Reply-tracking foundation: normalized inbound-email interface + threading via
   Message-ID / In-Reply-To / References (no fake inbox UI).
9. Email analytics: bounced/complained/opened/clicked/unsubscribed + rates.
10. `/connections/email` UI + 7-step add-account wizard + email campaign dashboard data.
11. RBAC: `email.*` permission set.
12. Public unsubscribe endpoint (no login) with abuse protection.

## 7. Database changes (all ADDITIVE — nothing dropped)

| Change | Reason |
|---|---|
| `campaign_recipients` + `opened_at, clicked_at, bounced_at, complained_at, tracking_key` | Phase 7 §18 timestamps + tracking-pixel/click resolution without exposing raw IDs |
| `campaigns` + `campaign_metadata` (JSON) | §31 campaign-level `track_opens / track_clicks / append_unsubscribe_footer` |
| `conversations` + `contact_email` | §32 inbound-email → conversation matching |
| NEW `email_tracking_events` | §29/§30 open/click evidence (indexes: recipient/event_type/created_at) |
| NEW `email_unsubscribe_tokens` | §13/§50 token_hash lookup (index token_hash, created_at) |

Existing `sending_accounts`, `campaign_templates`, `suppression_entries`,
`opt_out_records`, `campaign_events`, `provider_events`, `campaign_queue` are
**reused unchanged** (provider details live in metadata columns by design).

## 8. Configuration requirements (env; never committed)

`EMAIL_PROVIDER` (`smtp` | `email_api`), `SMTP_HOST/PORT/USERNAME/PASSWORD/SECURITY`
(TLS/STARTTLS/NONE), `EMAIL_API_BASE_URL/API_KEY/ACCOUNT_ID/REGION`,
`EMAIL_WEBHOOK_SECRET`, `QBIT_EMAIL_UNSUBSCRIBE_BASE_URL`,
`QBIT_EMAIL_DEFAULT_TRACK_OPENS/CLICKS`, `QBIT_EMAIL_MAX_PER_CAMPAIGN` (none planned),
webhook body/age limits reused from Phase 6. Secrets per sending account live in the
**encrypted credential vault** (Fernet/HKDF, `app/core/crypto.py`) — env values are
single-account bootstrap fallbacks only.

## 9. Security risks & mitigations carried into Phase 7

| Risk | Mitigation |
|---|---|
| SMTP credential leakage | write-only vault, masked hints, `SECRET_KEYS` log filter, no env-of-API echo |
| Header injection (From/To/Subject CRLF) | strict validation rejecting CR/LF in all header values (§39) |
| Template XSS | HTML sanitized with allowlist sanitizer at save + HTML-escaped variable substitution at render (§38) |
| Open redirect via click tracking | only http/https links rewritten; signed tracking URLs; scheme re-validated at redirect (§30) |
| Forged/replayed webhooks | HMAC-SHA256 over raw body + timestamp + `UNIQUE(provider, provider_event_id)` idempotency (§27/§28) |
| Unsubscribe token guessing | `secrets.token_urlsafe(32)`, SHA-256 hash stored, no IDs encoded in token (§51) |
| Duplicate emails on retry | idempotency key + SMTP "uncertain delivery" classified non-retryable (§20) |
| Suppression bypass | pre-send re-check in worker (already present) + opt-out evidence non-removable |

## 10. Compliance notes

- Individual-recipient delivery only — no CC batching, no header manipulation.
- Rate control is operational throttling only; no reputation evasion, no spam-filter bypass.
- No fake unsubscribe links, no fabricated opt-in, no fake analytics.
- Provider errors surfaced sanitized; restrictions never bypassed.

## Decision log

- `EmailProvider` stub (`interfaces.py`) is superseded by `providers/email/` package;
  the registry now registers real adapters under ids `smtp` and `email_api`
  (`channel="EMAIL"`), keeping the generic `email` interface registered for
  honest "not configured" reporting when accounts reference it.
- Bounce → recipient FAILED + `MESSAGE_BOUNCED` event + suppression
  (reason=BOUNCED) for hard bounces; soft bounces recorded, retried by the queue's
  existing policy. Complaint → `MESSAGE_COMPLAINED` event + suppression (COMPLAINED).
- Unsubscribe links require `QBIT_EMAIL_UNSUBSCRIBE_BASE_URL`; when unset the
  campaign validation reports a problem instead of generating fake links.
