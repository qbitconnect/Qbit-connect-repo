# PHASE 4 AUDIT — Lead Management + Import/Export + Data Workspace

Audit date: 2026-09-03 · Baseline: commit `1e63942` (Phase 3) on `main`

## 1. Current architecture (verified by code inspection)

| Layer | Status | Evidence |
|---|---|---|
| FastAPI app factory | ✅ working | `app/main.py` `create_app()`; app.state DI (db/storage/redis/audit/files/health/queue/scraper_registry) |
| Auth | ✅ working | Bearer JWT (API) + cookie session (UI); Argon2 hashes; login rate limiter |
| RBAC | ✅ working | 5 roles / 25 permissions seeded idempotently; `require_permission(code)` dep; `leads.view`, `leads.edit` already exist |
| PostgreSQL + Alembic | ✅ working | migrations `0001_core_foundation`, `0002_scraping_engine`; additive-only pattern with SQLite-safe bound timestamps |
| StorageService | ✅ working | path-traversal guarded, category roots (IMPORT/EXPORT/…), no raw client paths |
| FileService / ExportService | ✅ working | id→metadata→validated-path downloads; CSV/JSON/XLSX renderers (in-memory, bounded) |
| Redis / queue | ✅ working | `QueueBackend` protocol: Redis LIST/ZSET backend + in-process fallback (no Redis required) |
| Scraper engine | ✅ working | registry, 6 actors, JobEngine, runner, ResultPipeline, checkpoints, worker (`python -m app.worker`) |
| Result pipeline → leads | ✅ working | `ResultPipeline` batches → `Deduplicator` → `LeadService.create_or_update` (batch transaction, commit=False) |
| UI | ✅ working | server-rendered Jinja2, dark theme, cookie auth (`ui_user`), scraping cards/jobs pages |
| Audit logs | ✅ working | `AuditService.log(session, action, resource_type, resource_id, actor_user_id, metadata)` |
| Tests | ✅ 196 passing | fully isolated per-test SQLite + tmp storage; zero live external calls |

## 2. Existing lead/data functionality (reusable — do not duplicate)

- `models/scrape.py::Lead` — canonical lead table with normalized dedup keys
  (`email_norm`, `phone_norm`, `website_norm`, `name_key`), source provenance
  (`source`, `source_url`, `source_actor_id`, `source_actor_version`,
  `source_job_id`, `scraped_at`), lifecycle counters, `tags` JSON column,
  `status` string (currently `"active"`/`"archived"`), metadata JSONB.
- `services/leads.py::LeadService` — the only writer of lead rows; create_or_update
  with empty-field-safe merge, metadata history merge, archive, per-job listing.
- `services/scraping/lead_keys.py` — stable normalizers (email/phone/website/name_key). **Reuse for Phase 4.**
- `services/scraping/dedup.py::Deduplicator` — batched pipeline-level matching
  (HIGH = email/phone/website, MEDIUM = name+location, LOW = fuzzy name never auto-merged).
- `services/scraping/normalizer.py::normalize_item` — validation + canonicalization of raw items.
- `services/export.py::ExportService` — format renderers + FileService registration.
- Phase 3 scrape jobs already report records_found/saved/duplicate/failed.

## 3. Missing components (Phase 4 scope)

1. **Lead model gaps**: no `first_name/last_name`, `postal_code`, `industry`,
   `source_id`, `source_type`, `imported_file_id`, `import_batch_id`,
   `last_verified_at`, `quality_score`, no workflow status model
   (NEW/VERIFIED/…/ARCHIVED), no `merged_into_id`.
2. **No tag/note/activity tables** — tags are a JSON blob; notes/activities don't exist.
3. **No workspace-level dedup** — no persisted duplicate candidates, no merge, no merge history.
4. **No search/filter/sort/pagination API** for leads.
5. **No import system** (CSV/XLSX/JSON/JSONL, batches, mapping, error reports).
6. **No export history / streaming export** (current exporter materializes in RAM).
7. **No background data jobs** for large imports/exports (scrape worker is scrape-only).
8. **No leads UI** (`/leads`, detail, import, duplicates, quality, exports).
9. **No granular `leads.*` permissions** beyond view/edit.

## 4. Risks

- **Status migration**: existing rows use `active`/`archived`; Phase 4 maps them to
  `NEW`/`ARCHIVED` (information-preserving value rename, no data loss).
- **Tags column**: pipeline writes `tags` JSON directly. Phase 4 keeps the column as a
  denormalized mirror; `LeadTag`/`LeadTagAssignment` become the relational source of
  truth and the migration back-fills assignments from existing JSON values.
- **Memory**: current ExportService renders whole datasets in RAM → Phase 4 adds a
  chunked/streaming lead exporter (disk spool + `yield_per` batches).
- **Request blocking**: large imports/exports must not run in the request → size
  thresholds route big operations through QUEUED batches processed by the worker
  (DB-first claim, same pattern as scrape queue; works with in-process fallback).
- **SQLite test env**: migrations and queries must remain SQLite-compatible (existing convention).

## 5. Implementation plan

1. `models/lead.py`: LeadTag, LeadTagAssignment, LeadNote, LeadActivity,
   LeadMergeHistory, LeadDuplicateCandidate, SavedView, ImportBatch, LeadExportRecord;
   extend `Lead` in place. Migration `0003_lead_workspace` (additive; status value map;
   legacy tags back-fill; new permissions; new indexes).
2. `services/leads/` package (import-compatible with old `services/leads.py`):
   normalization, quality scoring (deterministic 0–100), activity, tags, views,
   DuplicateDetectionService, MergeService, extended LeadService
   (search/filter/sort/pagination/bulk), LeadIngestionService (centralized scraper→lead),
   LeadImportService, LeadExportService (streaming), data-job processor.
3. `api/v1/leads.py`: full §30 endpoint set; RBAC enforced server-side; pagination envelope.
4. UI: `/leads`, `/leads/{id}`, `/leads/import` wizard, `/leads/duplicates`,
   `/leads/quality`, `/leads/exports` + nav update. Dark QBIT visual language preserved.
5. Worker: data-jobs loop alongside the scrape loop (isolated process unchanged).
6. Tests: model/service/API/import/export/dedup/merge/bulk/security/perf (synthetic
   dataset, isolated env only). Docs: `docs/leads.md`, `docs/import-export.md`,
   `docs/data-workspace.md`; README; version 0.2.0 → 0.4.0.

## 6. Non-goals (enforced)

No marketing sending, no WhatsApp/email/SMS automation, no anti-ban/stealth/CAPTCHA
features, no cloud storage, no Node.js, no license servers, no DB resets, no fake UI data.

---

## 7. Validation outcome (post-implementation, same session)

The pre-existing WIP described above was validated line-by-line and verified
against a live running application before commit:

- 247/247 backend tests pass (47 lead-workspace tests added by the WIP, plus
  new regressions for migration-seeded RBAC rows, UI static pages, status
  validation and §26 records_updated).
- Live e2e smoke (isolated SQLite + temp storage, real uvicorn): 54/54 checks
  pass — migrations, seed, login/RBAC, lead CRUD, normalization, quality,
  search/filter/sort/pagination, injection rejection, bulk, tags, CSV import
  with mapping (2 imported / 2 invalid / 1 duplicate), rejected-rows download,
  duplicate scan + merge + soft-retire pointer, all 4 export formats, secure
  downloads, saved views, quality dashboard, all lead UI pages, scrape APIs,
  Phase 2 regressions (auth/me, settings, health).
- Bugs found and fixed during validation:
  1. **UUID storage mismatch** — migrations 0002/0003 inserted raw dashed
     uuid strings while SQLAlchemy `Uuid` stores hex32 on SQLite; any later
     ORM UPDATE of a migration-seeded permission crashed (StaleDataError).
     Fixed to `uuid4().hex` (valid on both SQLite and PostgreSQL) + regression
     test.
  2. **UI route shadowing** — `GET /leads/{lead_id}` was registered before the
     static pages, so `/leads/import|imports|duplicates|quality|exports` 422'd.
     Detail route moved after the static routes + regression test.
  3. Status validation tightened: custom statuses must match
     `^[A-Z][A-Z0-9_]{1,29}$`.
  4. §26 gap: scrape jobs had no `records_updated` counter — added column
     (migration 0003, additive), progress counter, pipeline hook, job UI.
