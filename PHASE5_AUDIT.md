# PHASE 5 AUDIT — QBIT CONNECT

Date: 2026-09-03 · Baseline: `main` @ `a313b2a` (docs: correct Phase 4 test count) · Version: `0.4.0`

Purpose: mandatory Step 0 audit before implementing **Phase 5 — Marketing Engine
Foundation**. Documents what already exists, what is reusable, what is missing,
integration points, risks and the migration plan.

---

## 1. Current repository state

- Working tree clean, `main` == `origin/main` @ `a313b2a`.
- Commit chain: `894afd9` (Phase 0 docs) → `947f44a` (Phase 2 foundation)
  → `1e63942` (Phase 3 scraper engine) → `448dead` (Phase 4 lead workspace)
  → `a313b2a` (docs fix).
- Backend tests: 219 passing (SQLite-per-test isolation; no production DB touched).
- Alembic revisions: `0001_core_foundation`, `0002_scraping_engine`, `0003_lead_workspace`.

## 2. Existing architecture (Phases 1–4) — DO NOT REBUILD

| Area | Location | Notes |
|------|----------|-------|
| App factory / DI | `app/main.py`, `app/asgi.py` | `create_app(settings, db=)`; services on `app.state` |
| Auth | `app/api/v1/auth.py`, `app/core/security.py` | JWT bearer for API, HttpOnly cookie for UI |
| RBAC | `app/models/rbac.py`, `app/services/rbac.py`, `app/api/deps.py` | `require_permission(code)` dependency; DB-seeded Permission/Role matrix; `load_user_permissions` |
| Errors | `app/core/errors.py` | uniform envelope `{success, error:{code,message,request_id}}`; `QBITError` hierarchy |
| DB | `app/db/session.py`, `app/db/base.py` | Async SQLAlchemy, naming convention, `uuid_pk()`, `timestamp_columns()` |
| JSON columns | `PortableJSON` (`app/models/scrape.py`) | JSON→JSONB variant (PostgreSQL JSONB, SQLite JSON) |
| Redis | `app/redis_client.py` | RedisManager; queue backend falls back to in-process when Redis absent |
| Scraper engine | `app/services/scraping/*`, `app/worker.py` | ActorRegistry, JobEngine, JobRunner, checkpoints, guarded-UPDATE claim + lease |
| Storage | `app/services/storage.py`, `app/services/files.py` | path-traversal-safe FileService, file metadata |
| Export | `app/services/export.py` | CSV/XLSX/JSON/JSONL streaming writer |
| Audit log | `app/services/audit.py`, `app/models/audit.py` | append-only, secret-redacting, never breaks main op |
| Leads (Phase 4) | `app/models/scrape.py` (Lead), `app/models/lead.py` | normalized keys `email_norm/phone_norm/website_norm/name_key`, status, quality_score, provenance, soft-merge |
| Lead services | `app/services/leads/*` | workspace (search/filter/sort/pagination), **filter engine with validated AND/OR groups**, saved views (PRIVATE/TEAM/GLOBAL), tags, notes, activity, dedup, merge, importer/exporter, quality, `DataJobWorker` background loop |
| Lead API/UI | `app/api/v1/leads.py`, `app/ui/leads.py`, `app/templates/leads/*` | dark enterprise theme, server-rendered thin client, real numbers only |
| Worker process | `app/worker.py` | scrape loop **plus** `_data_jobs_loop` (Phase 4 imports/exports) — the pattern to extend |

## 3. Marketing-relevant assets already present (reuse list)

1. **Saved views + filter engine** (`services/leads/views.py`, `filters.py`):
   validated filter groups over whitelisted fields — this IS the Audience Engine
   foundation. `SavedView.filters` stores the same JSON grammar campaigns need.
2. **RBAC infrastructure**: two marketing permissions already seeded
   (`marketing.view`, `campaign.view`, `campaign.create`) — Phase 5 adds the
   granular set below; roles update additively.
3. **Background job pattern**: `DataJobWorker` (DB-first claim, guarded UPDATE,
   stale-lease sweep to honest FAILED) — the campaign queue worker follows it.
4. **Queue backend**: `build_queue_backend(settings, redis)` — Redis or in-process.
5. **AuditService** for §46 (campaign/template/account/suppression changes).
6. **PortableJSON, uuid_pk, timestamp_columns** for all new tables.
7. **UI shell** (`base.html`, `qbit.css`, `_ctx`, `require_ui_permission`) —
   Campaigns nav item currently a disabled placeholder; becomes real in Phase 5.
8. **Lead statuses/tags** for audience definitions and eligibility metadata.

## 4. Existing connections model

`app/models/connection.py` exists (Phase 1–2 era) storing external account
connections. Phase 5 introduces a dedicated `sending_accounts` table because
marketing senders need channel/provider/health semantics the generic
connection row does not model. No conflict: sending accounts stand alone.

## 5. Missing marketing infrastructure (to build in Phase 5)

- `app/models/marketing.py`: Campaign, CampaignRecipient, CampaignEvent,
  CampaignTemplate, SendingAccount, SuppressionEntry, CampaignQueueItem,
  OptOutRecord.
- Alembic `0004_marketing_foundation` (additive only).
- `app/services/marketing/` package: audience, template, eligibility,
  suppression, queue, campaign, analytics, events, providers (base + channel
  interfaces + MOCK test provider), worker.
- `app/api/v1/campaigns.py`, `templates.py`, `sending_accounts.py`,
  `suppression.py` (endpoints per brief §29).
- `app/schemas/marketing.py` (Pydantic request models).
- UI: `app/ui/campaigns.py` + templates `app/templates/campaigns/*`.
- RBAC additions (see §6) + audit actions + structured logging events.
- Docs: `docs/marketing-engine.md`, `campaigns.md`, `templates.md`,
  `eligibility.md`, `providers.md`.

## 6. RBAC plan (brief §30)

New permission codes (additive; none removed):

- `campaigns.view/create/edit/validate/launch/pause/resume/cancel/export`
- `campaigns.analytics`
- `templates.view/create/edit/delete`
- `sending_accounts.view`, `sending_accounts.manage`
- `suppression.view`, `suppression.manage`

Legacy `campaign.view`/`campaign.create` remain valid (back-compat). Migration
inserts missing permission rows and role matrix rows idempotently
(`_insert_permissions_if_missing` pattern from 0003); `seed_rbac` updated in
lock-step so fresh installs and tests match existing databases.

## 7. Integration points

1. **Audience → Leads**: `AudienceService` resolves saved views / filter JSON /
   tags / statuses / explicit lead ids through the Phase 4 filter engine in
   **batched, streaming queries** (never loading all leads into RAM).
2. **Snapshot → campaign_recipients**: bulk inserts at launch; audience changes
   afterwards never mutate a launched campaign (reproducibility, brief §12).
3. **Eligibility → suppression/opt-out tables** checked before queue entry;
   suppressed contacts can never enter the queue (service + DB-level tests).
4. **Queue worker** runs inside the existing worker process next to the scrape
   and data-job loops (isolation rule); rate policy per sending account,
   conservative defaults.
5. **Provider abstraction**: `BaseMarketingProvider` with
   validate_configuration/validate_recipient/validate_message/send/get_status/
   handle_event/health_check. WhatsApp/Email/SMS interfaces registered but
   **unconfigured**; only the clearly-marked MOCK provider exists for tests.
   No provider secrets in API responses or logs (redaction reuse).

## 8. Risks

| Risk | Mitigation |
|------|-----------|
| Large audiences (10k+) blocking HTTP | snapshot + eligibility run as background job; batched inserts |
| Duplicate sends on worker restart | DB-unique idempotency key (campaign_id, recipient_id, message_version); queue item state machine |
| Accidental real sends in Phase 5 | providers must be explicitly configured; mock provider refuses to register unless QBIT_ENV=test or flag set; UI states "Provider not configured" |
| Destructive migration | additive-only migration; no DROP/TRUNCATE/RESET; verified against test DB |
| Permission drift between seed and live DB | idempotent permission back-fill migration mirroring `seed_rbac` |

## 9. Migration plan

`0004_marketing_foundation` (single revision, `down_revision="0003_lead_workspace"`):

1. Create 8 marketing tables + indexes + constraints (FK to leads where
   applicable; uniqueness: campaign recipient per campaign+lead, suppression
   address+channel, queue idempotency key).
2. Insert new permissions + role matrix rows (idempotent).
3. Downgrade drops only Phase 5 tables (created in this revision) — no other
   data touched.

## 10. Non-goals respected

No WhatsApp/Email/SMS real integration, no QR login, no WhatsApp Web
automation, no bulk unsanctioned sending, no anti-ban/stealth/CAPTCHA/proxy
evasion, no Node.js, no cloud storage dependency, no license server.

## 11. Conclusion

The existing architecture provides every foundation Phase 5 needs (auth, RBAC,
DB, Redis, storage, audit, worker, filter engine). Marketing infrastructure is
built as an additive module (`models/marketing.py`,
`services/marketing/*`, `api/v1/*`, `ui/campaigns.py`) following established
conventions. Implementation proceeds immediately after this audit.
