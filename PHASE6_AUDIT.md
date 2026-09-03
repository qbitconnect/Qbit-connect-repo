# PHASE 6 AUDIT — WhatsApp Business Provider Integration

Audited at: Phase 5 complete, commit `b65fe4e` ("feat(qbit-connect): add marketing engine foundation"),
version 0.5.0, 329 tests passing. This audit covers everything Phase 6 touches.

---

## 1. Existing Provider Interface (reusable as-is)

`backend/app/services/marketing/providers/base.py` — `BaseMarketingProvider`:

| Hook | Purpose | Phase 6 usage |
|---|---|---|
| `validate_configuration(config)` | config problem list (never echoes secrets) | real WhatsApp config validation |
| `validate_recipient(address)` | structural address check | E.164 phone validation |
| `validate_message(subject, body)` | channel rules (4096 chars, no subject) | kept |
| `send(account_config, recipient_address, subject, body, idempotency_key, metadata)` | one send → `SendResult` | **extended** with optional `template` payload kwarg (provider-agnostic signature preserved) |
| `get_status(...)` | best-effort status | kept (Cloud API status comes from webhooks) |
| `handle_event(payload)` | normalize event payload | kept |
| `health_check(account_config)` | account probe | real Graph API probe |

Supporting primitives already present and reused unchanged:
`SendResult` (ok / provider_message_id / error / error_code / error_class / status / metadata),
`ErrorClass` (TRANSIENT / PERMANENT / CONFIGURATION), `ProviderError`, `ProviderNotConfigured`,
`MarketingProviderRegistry` (mock gated to test envs; never auto-registered in production).

The current `WhatsAppProvider` (`interfaces.py`, provider_id `whatsapp_cloud`) is an honest
interface-only stub: every send fails with `PROVIDER_NOT_IMPLEMENTED`. Phase 6 replaces the stub
with the real adapter **keeping the same provider_id** so existing sending accounts remain valid.

## 2. Existing Sending-Account Architecture (reusable)

`SendingAccount` (models/marketing.py): multi-account by design — one row per WhatsApp Business
number. Fields: `name`, `channel`, `provider`, `identifier`, `display_identifier`,
`status` (PENDING/ACTIVE/INACTIVE/ERROR/SUSPENDED/DISCONNECTED — exactly the §6 set),
`health_status` (HEALTHY/DEGRADED/UNHEALTHY/UNKNOWN), `capabilities` (JSON),
`config_metadata` (JSON, NON-secret only), `last_health_check`. `to_public_dict()` never exposes
`config_metadata`. API `/api/v1/sending-accounts` rejects secret-like keys (`SECRET_KEYS`) and
refuses to store credentials. Campaign already carries `sending_account_id` → **one campaign →
one selected sending account** (§26) is already the data model; no account-pool randomization.

Missing for Phase 6: `credential_ref` (encrypted credential reference), `phone_number_id`,
`business_account_id` as first-class columns.

## 3. Marketing Engine Components (reusable unchanged)

- **CampaignService** — lifecycle machine (DRAFT→QUEUED→RUNNING→COMPLETED/…), validation report
  (channel/audience/template/account/provider/eligibility/schedule), launch pipeline
  (snapshot → eligibility → queue). Provider-agnostic: resolves providers through the registry.
- **EligibilityService** — ordered ladder: channel → lead usability → address present → address
  valid → suppression → opt-in (`metadata.marketing_opt_in == true`; scraped data is NOT consent).
  Batched; used by validation preview and launch.
- **SuppressionService** — global do-not-contact (EMAIL/PHONE/LEAD/CHANNEL) + opt-out evidence;
  batched IN-lookups; re-checked right before send in the worker (§13 satisfied).
- **QueueService** — DB-backed `campaign_queue`; idempotency UNIQUE (campaign_id, recipient_id,
  message_version) (§15 satisfied); guarded-UPDATE lease claim; transient retry with exponential
  backoff; permanent → FAILED immediately; per-account rate gate (operational throttling only).
- **EventService** — append-only CampaignEvent rows with `provider`, `provider_event_id`,
  redacted metadata (`core.logging.redact`).
- **CampaignWorker** (`worker loop`) — schedules → launches → claims queue batch → re-checks
  suppression → renders template (safe `{{var}}` substitution from lead allowlist) → provider.send
  → recipient transitions + events. Campaign loop isolated in `app/worker.py` `_campaign_loop`.
- **AnalyticsService** — computed from real recipient/event/queue rows (sent/delivered/read/
  replied/failed included → §34 UI values come free).
- **TemplateService** — safe `{{variable}}` engine; injection attempts rejected at validation;
  lead-field allowlist (`RENDER_VARIABLES`).

## 4. Existing Webhook / Event Architecture

- No public webhook endpoint exists yet. The only event ingress is the internal, JWT-protected
  `POST /api/v1/campaigns/events/provider` (normalized payload → `EventService.normalize_provider_event`).
- `CampaignEvent` has `provider_event_id` but **no uniqueness constraint** → webhook idempotency
  (§20) needs a dedicated storage layer with UNIQUE (provider, provider_event_id).
- Recipient state transitions are forward-only today (`_apply_event_to_recipient` compares enum
  order) but FAILED is not modeled as an explicit downward edge, and REPLIED exists. Phase 6 adds
  a strict state machine helper (§21) used by webhook processing.

## 5. Security / Secrets / RBAC / Audit (existing)

- Passwords: argon2id; tokens: HS256 JWT; permission enforcement `require_permission(code)` on
  every route; UI uses `require_ui_permission` mirrors. **Never frontend-only.**
- Audit service: structured `audit.log(...)` on every mutating action; metadata redacted.
- Secret hygiene: `SECRET_KEYS` redaction in structured logs + event payloads; sending-account API
  refuses secret-like config keys. **No encrypted-at-rest secret storage exists yet** — Phase 6
  must add it (see plan below).
- Config: pydantic-settings `QBIT_*`; production guards (strong secret key, PG required, no
  wildcard CORS, no mock providers/maps in production). `.env` git-ignored (verified).
- `whatsapp_cloud` already listed as a valid WHATSAPP provider in `CHANNELS` spec.

## 6. UI (existing)

Server-rendered dark theme (Jinja + static CSS). Nav in `base.html` currently shows
"Connections" as a disabled placeholder — Phase 6 activates it. Campaign wizard already renders
per-channel forms, eligibility preview, and honest "Provider not configured" blocks; campaign
detail shows real analytics + timeline. Sending accounts page exists at `/campaigns/accounts`
(reused patterns for the new `/connections` pages; no secrets displayed anywhere).

## 7. Missing WhatsApp Functionality (Phase 6 scope)

1. Real `WhatsAppProvider` adapter for the WhatsApp Business Cloud API (official/provider-supported
   only): send template messages, validate account/phone/business, health probe, template sync.
2. Encrypted-at-rest credential storage + vault reference on accounts (no plaintext tokens, no
   env-only workaround for multi-account).
3. Connection lifecycle API (`/connections/whatsapp/...`): create → validate → ACTIVE; validate
   must fail honestly and must NOT mark ACTIVE on provider failure (§5).
4. `WhatsAppErrorNormalizer` (§16): provider error codes → canonical classes (TRANSIENT/PERMANENT).
5. Webhook endpoints (§18/§19): GET verification challenge, POST with `X-Hub-Signature-256`
   validation, replay protection, idempotent processing (§20), strict recipient state machine (§21).
6. Provider template sync (§8/§9/§10): `provider_template_id`, provider status (PENDING/APPROVED/
   REJECTED/PAUSED/DISABLED), category, components, language, `last_synced_at`; campaign may only
   use templates the provider requires to be approved **when they are approved**.
7. Phone normalization service (§11): strict E.164, never guess country codes blindly.
8. Opt-in metadata enrichment (§12): `opt_in_status/source/timestamp/notes` recognized by
   eligibility (never fabricated).
9. Inbound message foundation (§22/§23/§24): normalized inbound events → lead match by
   sending account + normalized phone → Conversation + Message rows (no fake leads, no duplicates).
10. Launch gates (§27/§29): account capabilities check; unhealthy account → `SENDING_ACCOUNT_UNHEALTHY`,
    never queue.
11. UI: `/connections` hub + WhatsApp account wizard (§32) + account health (§35).
12. RBAC additions (§37), audit events (§38), env vars (§2), docs (§45).

## 8. Required Migrations (additive only — Alembic 0005)

- `sending_accounts`: + `credential_ref` (varchar 255, nullable), + `phone_number_id`
  (varchar 100, nullable), + `business_account_id` (varchar 100, nullable).
- `campaign_templates`: + `origin` (LOCAL/PROVIDER, default LOCAL), + `provider_template_id`,
  + `provider_status`, + `category`, + `components` (JSON), + `account_id` (FK sending_accounts,
  nullable), + `last_synced_at`, + `rejected_reason`.
- NEW `provider_credentials`: id, name (unique), provider, ciphertext (Text, encrypted JSON),
  key_version, metadata (JSON), created_by, last_used_at, last_rotated_at, timestamps.
- NEW `provider_events`: id, provider, provider_event_id, sending_account_id, category
  (DELIVERY/INBOUND/OTHER), event_type, provider_message_id, normalized (JSON, sanitized),
  raw_metadata (JSON, sanitized), received_at; UNIQUE (provider, provider_event_id).
- NEW `conversations`: id, channel, sending_account_id (FK), lead_id (FK nullable),
  external_contact_id, contact_phone, status (OPEN/PENDING/CLOSED), last_message_at, timestamps.
- NEW `messages`: id, conversation_id (FK), direction (INBOUND/OUTBOUND), provider_message_id,
  message_type, body (Text), status, metadata (JSON), created_at; index (conversation_id, created_at).
- NEW permissions: connections.view/create/edit/delete/validate/health/sync_templates,
  campaigns.whatsapp.launch, templates.whatsapp.view/manage, webhooks.whatsapp.receive (+ role
  matrix, additive; existing roles keep all current permissions).
- No DROP/TRUNCATE/RESET anywhere; downgrade removes only Phase 6 additions.

## 9. Required Environment Variables (documented in .env.example; never committed)

- `WHATSAPP_PROVIDER` (default `whatsapp_cloud`)
- `WHATSAPP_API_BASE_URL` (default `https://graph.facebook.com`)
- `WHATSAPP_API_VERSION` (default `v21.0`)
- `WHATSAPP_WEBHOOK_VERIFY_TOKEN` (app-level verification challenge token)
- `WHATSAPP_APP_SECRET` (app-level webhook signature fallback)
- Optional dev/bootstrap fallbacks: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_BUSINESS_ACCOUNT_ID`,
  `WHATSAPP_PHONE_NUMBER_ID` (per-account encrypted credentials ALWAYS take precedence; env
  fallbacks are a single-account convenience and are never returned by any API).

## 10. Security Risks Identified

| Risk | Mitigation in Phase 6 |
|---|---|
| Plaintext provider tokens | Fernet (key derived from QBIT_SECRET_KEY via HKDF-SHA256, dedicated salt+info) encrypted vault rows; ciphertext never returned by any API; `SECRET_KEYS` redaction covers logs/events |
| Weak key reuse | Vault refuses to run when QBIT_SECRET_KEY is the dev default in production (existing production guard) |
| Webhook forgery | HMAC-SHA256 `X-Hub-Signature-256` over raw body, constant-time compare; missing/invalid → 401; verification challenge token compare |
| Webhook replay / duplicates | UNIQUE (provider, provider_event_id) on provider_events; idempotent processing; duplicate payloads become no-ops |
| Secret leakage in logs | redact() applied to every event/log payload; client never logs headers/tokens; masked phone display (`••••1234`) |
| IDOR on accounts | every route behind `require_permission`; 404 for missing; no cross-tenant scoping needed (single-tenant install) but permission checks enforced server-side |
| Unhealthy-account sends | launch gate + worker pre-send health gate → SENDING_ACCOUNT_UNHEALTHY |
| Non-consented sends | eligibility ladder unchanged; opt-in metadata keys added; suppression re-check before send |
| Mock leakage to production | mock/whatsapp_mock providers remain `test_only`, registry refuses production; production guard already rejects mock accounts |
| Provider restriction bypass | explicitly none: errors surfaced honestly; rate limits respected via retry-after/backoff; no automation/QR/anti-ban code anywhere |

## 11. Implementation Plan (executed in this phase)

1. Migration 0005 (additive tables/columns/permissions) → models (`marketing.py` + new
   `messaging.py` for conversations/messages/provider_events).
2. `app/core/crypto.py` + `services/marketing/credentials.py` (CredentialVault).
3. `services/marketing/phone.py` (PhoneNormalizationService) + eligibility opt-in enrichment.
4. `services/marketing/providers/whatsapp/`: `client.py` (Graph API, httpx), `errors.py`
   (WhatsAppErrorNormalizer), `provider.py` (real WhatsAppProvider), `mock.py` (WhatsAppMockProvider,
   test-only). Registry wiring + `validate_send_requirements` hook on BaseMarketingProvider.
5. `services/marketing/connections.py` (ConnectionService: create/validate/health/disable/delete/
   sync_templates), `services/marketing/webhooks.py` (verification, signature, processing),
   `services/marketing/inbox.py` (inbound normalization, lead matching, conversations).
6. Campaign integration: provider template validation hook, capability + health gates,
   worker template-component rendering, per-recipient variable checks.
7. API: `api/v1/connections.py` + `api/v1/webhooks.py`; webhook routes public (signature-secured),
   everything else behind RBAC; audit events on all mutations.
8. UI: `/connections`, `/connections/whatsapp`, `/connections/whatsapp/new` (wizard),
   `/connections/whatsapp/{id}` (+ health display, templates, actions); nav item activated.
9. Tests (`tests/marketing/test_whatsapp_*.py`): ~90 tests across config/credentials/accounts/
   health/templates/normalization/eligibility/suppression/launch/provider/errors/retry/idempotency/
   webhooks/security. Mock transport for HTTP; WhatsAppMockProvider for end-to-end send flows.
10. Docs: docs/whatsapp-provider.md, whatsapp-connections.md, whatsapp-templates.md,
    whatsapp-webhooks.md; README; .env.example; version 0.5.0 → 0.6.0.

## 12. Explicit Non-Goals (enforced)

WhatsApp Web automation, QR-session scraping, personal WhatsApp automation, stealth/anti-ban,
CAPTCHA bypass, fingerprint spoofing, proxy/rate-limit evasion, unofficial session automation,
unauthorized bulk messaging, automatic opt-in generation, fake inbox UI. Provider restrictions are
surfaced verbatim (sanitized) and never bypassed.
