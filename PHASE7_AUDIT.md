# PHASE 7 AUDIT — Email Marketing Provider Integration

Date: 2026-09-03 · Branch: `main` · HEAD at audit start: `a313b2a`

---

## 1. Audit scope

Before coding, the following were inspected (per Phase 7 §STEP 0):

| Area (spec §STEP 0) | Expected from | Actual state in repo |
|---|---|---|
| Phase 5 Marketing Engine | Phase 5 | **NOT PRESENT** — no campaign/template/eligibility/queue code exists |
| Phase 6 WhatsApp Provider | Phase 6 | **NOT PRESENT** — no `BaseMarketingProvider`, no `WhatsAppProvider` |
| Campaign models | Phase 5 | **NOT PRESENT** |
| CampaignRecipient / CampaignEvent | Phase 5 | **NOT PRESENT** |
| Template system | Phase 5 | **NOT PRESENT** |
| SendingAccount | Phase 5/6 | **NOT PRESENT** (only the Phase 2 `connections` schema-only table) |
| EligibilityService / Suppression | Phase 5/6 | **NOT PRESENT** |
| Queue / Redis workers | Phase 3 | **PRESENT, reusable** (`services/scraping/queue.py`, `app/worker.py`) |
| Connections | Phase 2 | Schema-only `connections` table (category/provider/secret_ref) — reusable pattern |
| Inbox/conversation architecture | Phase 5/6 | **NOT PRESENT** |
| RBAC | Phase 2 | **PRESENT, reusable** — 25 permissions, `require_permission()` dependency |
| Secret management | Phase 2 | `secret_ref` column + audit redaction; **no encryption vault yet** |
| Audit logging | Phase 2 | **PRESENT, reusable** — append-only `AuditService` with redaction |

## 2. Verified baseline (Phase 0–4)

- Git history: `894afd9` (Phase 0 docs) → `947f44a` (Phase 2 core) → `1e63942`
  (Phase 3 scrapers) → `448dead` + `a313b2a` (Phase 4 leads). Working tree clean;
  local `main` == `origin/main`.
- 247 backend tests green at HEAD; Alembic at `0003_lead_workspace`.
- Conventions confirmed: UUID PKs via `uuid_pk()`, `timestamp_columns()`,
  `PortableJSON` (JSON→JSONB variant), unified `{success, data|error}` envelope,
  `QBITError` hierarchy, audit-on-mutation, test isolation via per-test SQLite +
  `create_app()` DI.

## 3. Gap vs. Phase 7 assumptions (honest finding)

The Phase 7 brief states "PHASE 1–6 are already implemented". The repository
audit proves otherwise: **Phase 5 (Marketing Engine) and Phase 6 (WhatsApp
Provider) were never committed** — no WIP, no stash, no other branch contains
them. Phase 7's target flow (Campaign → Template → Eligibility → Suppression →
SendingAccount → Provider → Queue → Events → Analytics) cannot exist without
that foundation.

**Decision (per the standing directive "implement completely, do not stop at
analysis" and the non-destructive rules):** implement the *minimum complete,
real* Phase 5/6-equivalent foundation Phase 7 requires — multi-channel
marketing engine core, provider abstraction (`BaseMarketingProvider`),
`WhatsAppProvider` (Cloud API adapter), sending accounts, eligibility,
suppression, campaign events, worker loop — and then build the full Phase 7
email stack on top. Nothing existing is deleted, reset or rebuilt; all changes
are additive (new tables/columns/permissions). No mock/fake behavior in
production paths.

## 4. Reusable components (kept as-is)

- **Queue pattern**: Redis LIST+ZSET backend with in-process fallback
  (`services/scraping/queue.py`) → mirrored by a dedicated marketing queue
  (`qbit:queue:marketing:*`), DB-first durable states.
- **Worker pattern**: recovery sweep + bounded concurrency + SIGTERM-safe loop
  (`app/worker.py`) → extended with a marketing delivery loop.
- **RBAC**: `PERMISSIONS` catalog + `ROLE_PERMISSIONS` matrix + migration
  seeding (Phase 4 pattern) → extended with `email.*`, `whatsapp.*`,
  `campaigns.*`, `suppression.*` codes.
- **Audit**: `AuditService.log()` with key redaction → reused for every
  Phase 7 mutation (never logs credentials).
- **Envelope/errors**: `QBITError` hierarchy + `error_envelope`.
- **Lead keys**: `leads.email_norm` / `phone_norm` already exist → reused for
  eligibility/suppression joins.

## 5. Missing email functionality (implemented in this phase)

EmailProvider abstraction; SMTP + generic Email API adapters; multi sender
accounts; sender/reply-to validation; email templates (subject/HTML/text +
whitelisted variables); HTML sanitization; header-injection protection;
unsubscribe tokens (hashed, public endpoint, no login); suppression +
unsubscribe-driven suppression; email eligibility chain with reason codes;
email normalization (`email` + `email_normalized` semantics via
`email_norm`); provider message id storage; idempotent sends
(campaign+recipient+message_version); TRANSIENT/PERMANENT error
classification + exponential backoff; bounce/complaint handling;
delivery-event normalization; generic webhook endpoint + signature/replay
protection + `provider_event_id` idempotency; optional open/click tracking
(safe redirects, http/https only); reply-tracking foundation (Message-ID /
In-Reply-To / References headers normalized into conversations); campaign
analytics (counts + rates from real events); rate control (operational
throttling only); RBAC; audit; observability events.

## 6. Database changes (migration `0004_marketing_email`, additive only)

New tables: `sending_accounts`, `marketing_templates`, `campaigns`,
`campaign_recipients`, `campaign_events`, `suppressions`,
`marketing_consents`, `unsubscribe_tokens`, `email_tracking_events`,
`provider_events`, `conversations`, `messages`.
New indexes per spec §50. New permission rows (RBAC) + role mappings.
**No table is dropped, truncated or reset; no existing row is deleted.**
The Phase 2 `connections` table remains untouched (Phase 7 uses the richer
`sending_accounts` table; `connections` stays for future non-marketing use).

## 7. Configuration requirements (env only; never committed)

`QBIT_MARKETING_*` rate/queue knobs, `QBIT_PUBLIC_BASE_URL` (unsubscribe +
tracking links), per-account provider credentials stored **encrypted at rest**
(Fernet, key derived from `QBIT_SECRET_KEY`) inside `sending_accounts.credential_ref`
→ `secret_vault` entries; SMTP/API env variables documented in `.env.example`
but per-account credentials live in the vault, not in process-wide env.

## 8. Security risks & mitigations

| Risk | Mitigation |
|---|---|
| Credential leakage via API/logs | Vault + never-return rule + `redact()` + masked UI |
| Header injection (CRLF) | Strict validation of From/To/Reply-To/Subject |
| XSS via template HTML | Allow-list sanitizer (BeautifulSoup-based, no new heavy deps) |
| Open redirect via click tracking | http/https-only scheme check, no `javascript:`/protocol-relative |
| Unsubscribe token guessing | `secrets.token_urlsafe(32)`, SHA-256 hash stored, no IDs encoded |
| Webhook forgery/replay | Shared-secret HMAC / provider scheme + timestamp window + `provider_event_id` uniqueness |
| Duplicate sends (restart/timeout) | DB idempotency key `(campaign_id, recipient_id, message_version)` + acceptance recorded before ACK; uncertain timeout ⇒ PENDING verification, never blind resend |
| IDOR on campaign/account APIs | Backend permission checks on every route (existing `require_permission`) |
| Fake consent | Opt-in is explicit data; scraped email ≠ consent; eligibility reason codes surfaced |

## 9. Implementation plan (executed after this audit)

1. Foundation: models + migration 0004 + RBAC extension + crypto vault +
   normalizers.
2. Providers: `BaseMarketingProvider`, `WhatsAppProvider`,
   `SMTPProvider`, `GenericEmailAPIProvider`, `MockEmailProvider`/`MockWhatsApp`
   (marked TEST ONLY), error/event normalizers, registry.
3. Services: sending accounts, templates, suppression, unsubscribe tokens,
   eligibility, campaign service, email delivery, webhooks, tracking,
   analytics.
4. Worker marketing loop; API routes; operator UI pages.
5. Tests (functional + security), docs (7 files), version bump, commit/push.
