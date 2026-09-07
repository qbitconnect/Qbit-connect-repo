# PHASE 8 — REPOSITORY AUDIT (STEP 0)

Phase 8 target: **Unified Inbox + Conversations** (WhatsApp + Email in one
workspace). This audit was performed before any Phase 8 code was written.

Repository state at audit time: HEAD `9570ffb` ("docs: correct Phase 7 test
count (536)"), version **0.7.0**, working tree clean.

---

## 1. Phase 1–7 implementation status (verified)

| Phase | Content | Evidence |
|---|---|---|
| 0–1 | Architecture docs, core DB + storage | migrations `0001_core_foundation`, `StorageService` |
| 2 | Modular scraper actor engine | `app/scrapers/`, `app/services/scraping/*`, `ScrapeWorker` |
| 4 | Lead management workspace | `app/models/lead.py`, `app/services/leads/*`, Leads UI |
| 5 | Marketing engine foundation | `app/models/marketing.py`, `CampaignService`, `QueueService`, `CampaignWorker` |
| 6 | WhatsApp Business provider | commit `a764d3c`, `WhatsAppProvider`, webhook pipeline, `PHASE6_AUDIT.md` |
| 7 | Email marketing provider | commit `f48cf7f`, SMTP/Email-API adapters, reply-tracking, `PHASE7_AUDIT.md` |

Both Phase 6 (WhatsApp) and Phase 7 (Email) are complete and committed. The
Phase 8 preconditions hold.

## 2. Existing conversation architecture (reusable as-is)

- **Models** (`app/models/messaging.py`, migrated in `0005_whatsapp_provider`):
  `Conversation` (channel, sending_account_id, lead_id, external_contact_id,
  contact_phone, contact_email, status PENDING/OPEN/CLOSED, last_message_at),
  `Message` (conversation_id, direction INBOUND/OUTBOUND, provider_message_id,
  message_type, body, status RECEIVED/SENT/DELIVERED/READ/FAILED, metadata,
  created_at), `ProviderEvent` (UNIQUE(provider, provider_event_id) — the
  idempotency gate), `ProviderCredentials` (Fernet vault).
- **WhatsApp inbound pipeline (LIVE)**: `POST /api/v1/webhooks/whatsapp`
  (raw-body HMAC `X-Hub-Signature-256`, size cap, stale-event window) →
  `WhatsAppWebhookService.process_payload` → `WhatsAppEventNormalizer` →
  idempotent `ProviderEvent` → `InboxService.record_inbound_message` →
  conversation + message → `link_reply_to_campaign` (REPLIED + event).
- **Email inbound foundation (service only)**: `EmailInboundService`
  (`app/services/marketing/email_tracking.py`) normalizes
  from/to/subject/body + Message-ID/In-Reply-To/References into
  conversation/message rows keyed by (sending_account, normalized contact
  email). **No transport calls it yet** — Phase 8 must add the inbound route.
- **Email delivery webhooks (LIVE)**: `POST /api/v1/webhooks/email/{provider}`
  with `X-QBIT-Signature` + replay window → `EmailWebhookService` → recipient
  state + suppression.

## 3. Reusable components (no rebuild)

- Provider abstraction: `BaseMarketingProvider.send(account_config, recipient_address, subject, body, idempotency_key, credentials, template)` — used unchanged for inbox replies.
- Credential resolution: `resolve_account_credentials` / `resolve_email_credentials` (vault → env, per-call secrets only).
- Error normalization: `WhatsAppErrorNormalizer`, `EmailErrorNormalizer` (TRANSIENT/PERMANENT/CONFIGURATION).
- Phone/email normalization: `normalize_recipient_phone`, `normalize_lead_phone`, `normalize_email`.
- Recipient state machine (forward-only, out-of-order safe): `services/marketing/state.py`.
- Suppression: `SuppressionService.is_suppressed(channel, email, phone, lead_id)`.
- RBAC: `require_permission(code)` dependency; permission catalog + role matrix in `services/rbac.py` (**no `inbox.*` codes yet**).
- Audit: `AuditService.log(...)` (redacting, failure-isolated).
- Queue/worker pattern: DB-backed items with lease/attempts/backoff consumed by `CampaignWorker` inside `app/worker.py` loops (HTTP handlers never send).
- Pagination/envelope conventions: `{"success", "data": {items, total, page, page_size, total_pages}}`, offset pagination; QBITError error envelope.
- UI conventions: Jinja2 + `qbit.css` dark theme, cookie-session UI deps (`ui_user_for`), polling with `fetch` (no realtime system exists — polling is the established fallback).
- Storage: `StorageService` + `attachments/` category dir; `nh3.sanitize_html` from `email_compose` for safe email HTML.

## 4. Missing Inbox functionality (Phase 8 greenfield)

1. Conversation REST API — none exists (`rg conversation app/api/` → empty).
2. `inbox.*` permissions, role-matrix entries, seeds.
3. Conversation workflow fields: WAITING/RESOLVED statuses, priority,
   assignment (assigned_user_id), unread_count, last_inbound/outbound_at,
   closed_at, subject, match status (MATCH_REVIEW_REQUIRED).
4. Message display fields: sender/recipient/subject/timestamps
   (sent/delivered/read/failed_at), external_message_id (client idempotency).
5. `ConversationNote`, `ConversationEvent` (activity + assignment history),
   outbound reply queue (`inbox_outbox`) + worker loop.
6. Email inbound transport (provider/mailbox webhook route feeding
   `EmailInboundService`).
7. Reply pipeline through provider adapters with idempotency + retry +
   WhatsApp messaging-window rules (24h customer service window → template
   required outside it; no bypass).
8. Delivery-status updates applied to Message rows (WhatsApp statuses
   currently update CampaignRecipient only).
9. Campaign → conversation linkage (outbound campaign messages appearing in
   the thread).
10. `/inbox` UI (nav item is a disabled placeholder), sidebar unread badge.
11. Inbox tests, smoke script, docs, version bump.

## 5. Required migrations (additive-only, `0007_inbox_conversations`)

- `conversations` += subject, priority, assigned_user_id (FK users SET NULL),
  assigned_team_id (reserved, no team table exists yet), last_inbound_at,
  last_outbound_at, unread_count (default 0), closed_at, match_status.
- `messages` += lead_id (FK leads SET NULL), external_message_id, sender,
  recipient, subject, sent_at, delivered_at, read_at, failed_at.
- New tables: `conversation_notes`, `conversation_events`, `inbox_outbox`.
- New indexes: conversations(status / assigned_user_id / priority /
  unread_count / channel / last_message_at exists), messages(external_message_id
  unique per conversation), inbox_outbox(idempotency_key UNIQUE,
  status+available_at), notes/events indexes.
- Permission seed rows for `inbox.*` (same pattern as `0006_email_provider`).
- `tests/test_migrations.py` table-whitelist must gain the new set.

No destructive operations are required. Existing rows keep valid statuses
(PENDING/OPEN/CLOSED ⊂ new enum set); unread_count backfills to 0.

## 6. Required APIs

Per Phase 8 spec §45–§46 under `/api/v1/inbox` (conversations CRUD-lite,
messages list/post, read/unread, status, priority, assign, notes, activity,
link-lead, create-lead, unread-count, search, bulk actions) + a retry endpoint
for failed replies. UI data endpoints under `/ui/inbox/*` (cookie session,
same service layer).

## 7. Required UI

`/inbox` three-pane workspace (list / thread / lead context), filters, search,
composer with template fallback outside the WhatsApp window, notes, lead
context panel, empty/error states, sidebar unread badge (polling). Navigation
placeholder replaced by the real entry.

## 8. Security risks & controls carried into Phase 8

- **XSS / HTML injection** — inbound email HTML must be sanitized (nh3
  allowlist) and rendered sandboxed; plain-text fallback provided.
- **SSRF** — no server-side media download is implemented in Phase 8; if
  added later it MUST reuse Phase 3 SSRF guards. Metadata-only media
  foundation (media_type/provider_media_id/filename/mime/size in metadata).
- **IDOR / RBAC bypass** — every route enforces `inbox.*` permissions
  server-side; visibility scope (ALL vs ASSIGNED_ONLY) enforced in SQL, not
  the UI. No team model exists yet → TEAM visibility is reserved.
- **Webhook spoofing** — inbound email route reuses the Phase 7 HMAC +
  replay-window scheme; WhatsApp keeps `X-Hub-Signature-256`.
- **Secret exposure** — replies resolve credentials per call; nothing secret
  is logged or returned; audit metadata is redacted.
- **Duplicate sends** — reply idempotency key = conversation_id +
  client_message_id (UNIQUE on outbox); retries re-use the same message row.
- **Provider compliance** — WhatsApp replies respect the 24h customer-service
  window (template required outside it); provider errors are surfaced
  honestly; rate control remains operational throttling, never bypass.

## 9. Decisions recorded

1. **Teams**: no team model exists; `assigned_team_id` is reserved (nullable,
   unused) and assignment ships for users only. No team names are hard-coded.
2. **Saved inbox views**: Phase 4 `SavedView` is lead-specific
   (entity-coupled validation); reusing it would duplicate filter semantics.
   The inbox ships rich filters instead; saved views stay a documented future
   extension (spec §13 is conditional — "if reused safely").
3. **Email inbound transport**: a signature-verified inbound webhook
   (`POST /api/v1/webhooks/email-inbound/{provider}`) using the Phase 7
   HMAC contract. IMAP polling would add credentials/state beyond this
   phase; the webhook is the provider-style event path the platform already
   standardizes on.
4. **Notifications**: in-app unread indicators (sidebar badge + conversation
   unread counts) satisfy the spec minimum; no parallel notification system.
5. **Realtime**: polling fallback per spec §37 (the project's existing
   mechanism); no competing realtime stack introduced.
