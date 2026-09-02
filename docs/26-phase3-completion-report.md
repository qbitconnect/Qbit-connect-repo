# Phase 3 Completion Report — Scraper / Actor Engine

**Commit:** `feat(qbit-connect): add modular scraper actor engine`
**Date:** 2026-09-02
**Baseline:** Phase 2 (commit 947f44a) — verified intact before any change; all 109 Phase 2 tests passing pre-implementation (after fixing the pre-existing WIP breakages described in §3).

---

## 1. Architecture Implemented

The Phase 3 brief is implemented as a **plugin-based actor platform**:

```
Frontend (cookie UI / API client)
    ↓  POST /api/v1/scrapers/{id}/jobs  (strict input validation, RBAC)
FastAPI (app.state.scraper_registry + app.state.queue)
    ↓
JobEngine  ── scrape_jobs row COMMITTED FIRST (DB-first, doc 09 §1)
    ↓
QueueBackend (Redis LIST/ZSET ── or InProcess fallback when REDIS_URL unset)
    ↓
Worker process (python -m app.worker, isolated from API — brief §15)
    ↓ JobRunner.claim (atomic QUEUED/PAUSED → RUNNING + lease)
ScraperActor.run(ctx)  — async GENERATOR, streaming (§25)
    ↓ items
ResultPipeline: normalize → dedup → LeadService upsert → counters → JSONL writers
    ↓
scrape_jobs counters + scrape_job_events + scraper-results/{actor}/{job}/ JSONL
```

Core engine contains **zero source-specific logic** (§2). Actors self-register via
`app/scrapers/bootstrap.py`; adding actor #7+ = one import + one list entry.

## 2. New Files

```
backend/app/scrapers/
    bootstrap.py                       actor registration + feature flags (§50)
    core/base.py                       ScraperActor contract (§3), ValidationReport
    core/context.py                    ScraperContext, JobLimits, control plane (§10)
    core/exceptions.py                 §40 exception taxonomy + ScraperLimitReachedError
    core/http.py                       PolicyHttpClient: rate limit, retries+jitter,
                                       response caps, redirect re-validation, robots
    core/netguard.py                   SSRF/private-network guard (§41), canonical URLs
    actors/website/{actor,schemas,parser}.py + README.md     (§26)
    actors/email_finder/{actor,schemas}.py   + README.md           (§27)
    actors/google_maps/{actor,schemas,provider,mock_provider}.py + README.md (§28, §47)
    actors/business_directory/{actor,schemas,adapters}.py + README.md        (§29)
    actors/universal/{actor,schemas}.py      + README.md                 (§30)
    actors/public_data/{actor,schemas}.py    + README.md                 (§2)
backend/app/services/scraping/
    registry.py   ActorRegistry (register/discover/health/summary — UI-independent §5)
    engine.py     JobEngine (legal transitions, controls, recovery) + config whitelist
    queue.py      QueueBackend protocol + Redis + InProcess backends (§14)
    runner.py     JobRunner (claim/execute/limits/heartbeat/retry/finalize) (§11–§19)
    pipeline.py   ResultPipeline (§20, batched transactions §44)
    dedup.py      confidence-based deduplicator (§21)
    normalizer.py canonical lead normalizer + NormalizedLead schema (§9)
    lead_keys.py  email/phone/website/name-key normalization
    progress.py   honest cumulative counters, percent only when total known (§38)
    events.py     aggregated event reporter (§13 — no event-row floods)
    checkpoints.py Postgres-persisted checkpoints, pruned, throttled (§18)
    result_files.py streaming JSONL writers raw/normalized/errors (§24, §45)
backend/app/worker.py                worker entrypoint + graceful shutdown + sweeps
backend/app/api/v1/scrapers.py       registry endpoints (§51)
backend/app/api/v1/scrape_jobs.py    job endpoints + export handoff (§37, §51)
backend/app/schemas/scraping.py      API request/response schemas
backend/app/ui/__init__.py           cookie-session UI router (§33–§37)
backend/app/templates/{base,login,403}.html
backend/app/templates/scraping/{index,detail}.html
backend/app/templates/jobs/{index,detail}.html
backend/app/static/css/qbit.css      dark control-center theme
backend/tests/scrapers/              conftest + 5 test modules (see §16)
backend/tests/test_ui.py             UI flow tests
backend/tests/test_scraping_api.py   API lifecycle/RBAC/export tests
docs/26-phase3-completion-report.md  this document
```

## 3. Modified Files

| File | Change |
|---|---|
| `app/models/scrape.py` | +QUEUED→PAUSED transition, +paused/cancelled in `to_public_dict` |
| `app/models/__init__.py` | export scraping models (WIP completed) |
| `app/services/rbac.py` | +4 permissions: scraping.pause/cancel/export/manage (WIP completed) |
| `app/services/leads.py` | `commit=` parameter → one transaction per pipeline batch (§44) |
| `app/core/config.py` | +16 scraper/worker/provider knobs, `scraper_allowed_ports()`, `scraper_disabled_actors()`, production guards |
| `app/main.py` | registry+queue on app.state, lifespan health snapshot, 2 routers, UI router + UiRedirect handler, /static mount |
| `app/api/v1/…` | no changes to Phase 2 routers (additive only) |
| `alembic/versions/0002_scraping_engine.py` | fixed `sa.uuid.uuid4()` → `uuid.uuid4()`; `NOW()` → bound `:ts` (SQLite-compatible) |
| `docker-compose.yml` | +qbit-worker service (same image, `python -m app.worker`) |
| `requirements.txt`, `pyproject.toml` | +beautifulsoup4, +jinja2 |
| `.env.example`, `backend/.env.example` | +scraper/worker/provider variables |
| `README.md` | status block → Phase 3 |
| `tests/test_rbac.py`, `tests/test_migrations.py` | expectations updated for the 4 new permission rows + 4 new tables |

## 4. Database Migrations

`0002_scraping_engine` (0001_core_foundation ← 0002). **Additive only** — creates 4
tables + indexes + 4 idempotent permission rows with role links; touches nothing
from Phase 2. Verified by `tests/test_migrations.py` (upgrade → re-upgrade with
probe data intact → downgrade → upgrade again).

## 5. New Tables

| Table | Purpose |
|---|---|
| `scrape_jobs` | job rows: actor+version, JSON input/config, status, cumulative counters, lease, stop_requested, timestamps |
| `scrape_job_events` | stage events + AGGREGATED item events (ITEMS_BATCH) — no millions of rows (§13) |
| `scrape_job_checkpoints` | durable resume cursors (small JSON), newest-5 kept per job (§18) |
| `leads` | canonical lead store with normalized dedup keys + full source tracking (§22, §23) |

## 6. Actor Registry

`ActorRegistry`: register/unregister/discover/get/list/health_check/summary.
Contract validation on register (VALIDATED state); feature flags produce
DISABLED; dependency loss produces DEGRADED — no fake states (§4, §49, §50).
UI-independent; mounted on `app.state.scraper_registry` and used verbatim by the
worker.

## 7. Actors Implemented (6)

| Actor | id | Notes |
|---|---|---|
| Google Maps | `google-maps` | **Provider-based only** (§28): MapsProvider protocol → HttpMapsProvider (operator's compliant endpoint) / MockMapsProvider (tests/dev, refused in production). No provider → DEGRADED + refuses to run with a clear error. No CAPTCHA/evasion by design. |
| Website | `website` | Same-domain BFS crawl, contact-page hints, public emails/phones/social/address, robots.txt enforced, depth/page limits, frontier checkpoints. |
| Email Finder | `email-finder` | Public email discovery, type+confidence classification, provenance, `consent: not_implied` on every record (§27). |
| Business Directory | `business-directory` | DirectorySource adapter protocol + declarative `generic` adapter (CSS selectors from job input) — no hardcoded sites (§29). |
| Public Data | `public-data` | Open-data JSON/CSV ingestion with declarative field mapping, streamed. |
| Universal Web | `universal-web` | Field→selector→attribute extraction foundation, optional list items + pagination, deterministic (§30). |

Each actor package carries its own README documenting input/output/safety.

## 8. Job System

QUEUED → RUNNING → PAUSED/COMPLETED/FAILED/CANCELLED with a legal-transition map
enforced by JobEngine (FAILED→QUEUED operator retry included). Controls are
cooperative: API writes DB `stop_requested` (durable) + queue control key (fast
path); actors stop at safe points. QUEUED jobs can be paused; PAUSED jobs are
cancelled immediately; resume re-enqueues and the worker's atomic claim performs
PAUSED→RUNNING (status reflects worker reality).

## 9. Queue / Worker

Redis backend (LIST + ZSET for delayed retries + control/lease keys) with an
InProcess fallback keeping the platform local-first without Redis. Worker:
`python -m app.worker` — startup recovery sweep (expired RUNNING leases →
re-queue from checkpoint with `resumed_count++`; stale QUEUED re-enqueue),
bounded concurrency, lease heartbeats, SIGTERM → checkpoint+PAUSE (never fail),
periodic re-sweep. Unknown/disabled actors fail jobs honestly
(SCRAPER_CONFIGURATION_ERROR), never silently (§55).

## 10. Storage Integration

Job results stream to `QBIT_DATA_DIR/scraper-results/{actor_id}/{job_id}/raw.jsonl
| normalized.jsonl | errors.jsonl` — append-only writers, 200-record flush
batches, memory-flat for million-record jobs (§24, §45). Exports are stored via
the Phase 2 StorageService EXPORT category.

## 11. Lead Normalization

`normalize_item()` maps any raw item to the canonical §9 schema: whitespace
collapse, E.164-style phones (leading + preserved), lowercase emails, bare-host
websites, unknown keys → `metadata`, malformed email/phone → `metadata.unparsed`
salvage (never lost silently). Derived dedup keys (email_norm/phone_norm/
website_norm/name_key) ride along; invalid records are REJECTED pre-dedup and
counted as failed (§20). Three normalizer bugs found by tests were fixed
(scraped_at JSON string vs datetime, phone salvage inverted, phone + prefix).

## 12. Deduplication

Confidence matrix per brief §21: exact email/phone/website-host = HIGH (merge:
fill empty fields only, merge metadata with history, bump seen_count);
name+location = MEDIUM (auto policy: insert with `possible_duplicate_of` flag —
information-preserving; strict policy: merge); fuzzy name = reported, never
merged. Batched lookups with a flush-before-lookup correction (autoflush is off
platform-wide; without it, same-batch duplicates all inserted) and a
matches-only LRU (caching misses poisoned repeated-item handling).

## 13. Security Protections

- **SSRF (§41):** every URL + every redirect hop validated; scheme allowlist
  (http/https), userinfo refused, port allowlist, DNS resolution with
  loopback/RFC1918/link-local/CGNAT/reserved/multicast/0.0.0.0/cloud-metadata
  refusal, IPv6 equivalents, literal-IP check WITHOUT DNS (defense in depth),
  `localhost` blocked outright. `ALLOW_PRIVATE_TARGETS` refused in production
  config validation. Known limitation (documented in netguard + actor READMEs):
  DNS rebinding is out of scope; run workers without internal network access for
  hard guarantees.
- **Job config whitelist:** 9 keys with hard ranges; unknown keys → 422. No
  client-side SSRF-policy tampering.
- **RBAC (§52):** scraping.view / run / pause / cancel / export / manage
  enforced server-side on every endpoint (API via `require_permission`, UI via
  cookie+permission checks). VIEWER (read-only) verified in tests.
- **Auditing:** job create/pause/resume/cancel/retry/export audited via the
  Phase 2 AuditService.
- **Resource safety (§32, §43):** wall-clock deadline → pause-at-checkpoint;
  max_records/max_pages → clean COMPLETED with LIMIT_REACHED event; response
  size cap (declared + streamed), request timeouts, bounded pages/depth/items,
  worker concurrency cap. Zip-bomb/huge-page protection in PolicyHttpClient.
- **Responsible scraping (§17):** per-host token-bucket rate limiting +
  inter-request delay, robots.txt respected through the policy client (async,
  policy-enforced — fixed from the WIP's raw sync fetch that bypassed SSRF
  rules), no CAPTCHA/anti-bot/stealth/IP-rotation anywhere.

## 14. API Endpoints (all under /api/v1)

```
GET  /scrapers                     list cards (status, schema, capabilities)
GET  /scrapers/health              refresh + health map + summary
GET  /scrapers/{id}                detail
POST /scrapers/{id}/validate       strict input+config validation (no side effects)
POST /scrapers/{id}/jobs           create + enqueue (audited)
GET  /scrape-jobs                  list (status/actor/search/pagination)
GET  /scrape-jobs/{id}             detail
POST /scrape-jobs/{id}/pause       scraping.pause
POST /scrape-jobs/{id}/resume      scraping.pause
POST /scrape-jobs/{id}/cancel      scraping.cancel
POST /scrape-jobs/{id}/retry       scraping.run
GET  /scrape-jobs/{id}/logs        event stream (type/after filters)
GET  /scrape-jobs/{id}/results     paged leads
GET  /scrape-jobs/{id}/results/export?format=csv|xlsx|json   §37 handoff
```

## 15. UI Pages (cookie-auth, dark control-center theme)

- `/login` (+ logout) — HttpOnly SameSite=Lax session cookie wrapping the API JWT.
- `/scraping` — SEARCH SCRAPERS cards: icon, name, description, version,
  category, live status badge, Run button; search filter; disabled actors
  not runnable (§33, §50); DEGRADED cards communicate the missing dependency.
- `/scraping/{id}` — detail: description, version, capabilities, schema-driven
  input form, collapsible Advanced settings, [Validate] (AJAX) + [Run] (§34).
- `/scraping/jobs` — status filter tabs (All/QUEUED/RUNNING/PAUSED/…), search,
  table (id, scraper, status, progress, records, created, started, duration),
  pagination (§36).
- `/scraping/jobs/{id}` — status, progress (honest: percent only when a total
  exists, otherwise raw record counts), counters, elapsed, Pause/Resume/Cancel/
  Retry, live log via 4s JSON polling of `/scraping/jobs/{id}/live` (lightweight,
  no full-page refresh, §35/§38), results section on terminal state with
  View-Leads table + CSV/XLSX/JSON export buttons (§37).
- `/403` — permission-denied page.
- Frontend holds zero execution logic — forms POST to /ui endpoints that call
  the same services (§53).

## 16. Tests

`backend/tests/scrapers/` (new):
- `test_actor_contract.py` — registration, duplicates, contract validation,
  metadata shape, input validation, feature flags, google-maps DEGRADED/refusal.
- `test_netguard.py` — 14 blocked-URL classes, private-IP matrix, DNS failure,
  allowed_hosts escape hatch, canonical URLs, same-domain logic, opt-in flag.
- `test_normalizer_dedup.py` — normalization, salvage, rejection, dedup
  confidence matrix, lead create/merge/flag semantics.
- `test_runner_pipeline.py` — full JobRunner: complete+persist+files, same-batch
  dedup, invalid counting, PAUSE→checkpoint→RESUME (totals cumulative, no
  re-crawl), cooperative CANCEL, retry-then-fail, no-retry-for-validation,
  max_records clean stop + LIMIT_REACHED, limit checks, lease-expiry recovery,
  config whitelist.
- `test_actors_http.py` — website crawl/extraction/domain restriction/robots/
  page limits/huge-response containment, email-finder classification+provenance,
  universal selectors, public-data JSON+CSV, google-maps mock provider +
  no-provider refusal — all over httpx.MockTransport, zero live sites (§46, §47).

`tests/test_ui.py` — anonymous redirect, login success/failure, cookie flags,
cards, search, schema form, validate, run→job creation, invalid run 422,
job list/detail, pause via UI, viewer read-only enforcement.

`tests/test_scraping_api.py` — auth, health (READY/DEGRADED), validation errors,
create happy path, invalid input/config, disabled actor 409, full lifecycle
(pause/resume/logs/results/export-guard/cancel), export handoff for CSV/JSON/
XLSX + download through the Phase 2 files API.

## 17. Test Results

```
$ python -m pytest tests/ -q
........................................................................ [ 73%]
....................................................                     [100%]
196 passed
```

(109 Phase 2 tests intact + 87 new Phase 3 tests.) `compileall` clean;
docker-compose YAML validated; Alembic up/down cycle verified on SQLite in
tests (PostgreSQL parity via portable JSON variant + bound timestamps).

## 18. Performance Considerations

- HTTP-first (requests via reused AsyncClient pool); Playwright deliberately
  deferred until a JS-rendered need is proven (§31) — documented per-actor.
- Streaming everywhere: generator actors, batched pipeline transactions (one
  commit per 100), batched event rows, interval-based counter flushes (3s),
  append+flush JSONL writers — no unbounded lists (§25, §44, §45).
- Indexed hot paths: status+created, actor+created, dedup key columns, job_id
  indexes on events/checkpoints/leads.
- Token-bucket per-host limiter + global semaphore; concurrency configurable;
  single robots fetch cached per host.
- Checkpoints pruned to newest 5 per job; events aggregated; progress writes
  coalesced — DB write volume stays proportional to job count, not record count.

## 19. Known Limitations

1. **DNS rebinding**: targets are validated at request time; a hostile
   authoritative DNS could return differing addresses between validation and
   connection. Mitigation for hard guarantees: run workers without internal
   network access (documented in netguard + website actor README).
2. **Google Maps without a provider** is DEGRADED by design — the operator must
   configure a compliant data endpoint; there is intentionally no direct-scrape
   fallback (§28, §55).
3. **Export materializes rows in memory** through the Phase 2 ExportService
   (`list(rows)`); fine for default max_records (10k) — very large exports need
   a streaming renderer (Phase 2 service limitation, not duplicated here).
4. **UI auth** uses the stateless JWT cookie (SameSite=Lax mitigates CSRF);
   server-side sessions/revocation remain the documented future auth phase.
5. **WebSocket/SSE progress** deliberately avoided in favor of a 4s polling JSON
   endpoint — the lightweight mechanism appropriate to the current stack (§35);
   SSE can replace it without UI restructuring.
6. Pause requires actors to reach a safe point; website/email-finder/universal/
   business-directory/public-data pause between pages/items; the maps actor
   pauses between provider pages. Actors that cannot pause declare it
   (`supports_pause=False`) for the UI (none currently required to).
7. Single-worker healthcheck imports the module (compose) — a real liveness
   probe on a worker metrics port arrives with the operations phase.

## 20. Remaining Work for Phase 4 (suggested)

1. Lead management depth: UI browse/search/filter/merge UI, tag management,
   archive flows, bulk operations on the leads table.
2. Connections module (schema exists since Phase 2): encrypted credential
   storage + provider adapters.
3. Server-side auth sessions/revocation (retire stateless-only tokens).
4. Redis-backed presence + SSE progress channel (optional upgrade over polling).
5. Scheduled/recurring scrape jobs + notify-on-completion hooks.
6. Playwright optional dependency behind the same actor contract for
   JS-rendered targets, with strict browser-instance caps (§31).
7. Export streaming renderer for very large lead sets.
8. Metrics endpoint for worker/queue depth (Prometheus-style) + alerting.

---

## Phase 3 Completion Checklist (brief §56)

```
[x] ScraperRegistry works                 [x] Lead normalization works
[x] Actor contract works                  [x] Deduplication works
[x] Scraper metadata works                [x] Local result files work
[x] Input schemas work                    [x] Website scraper works
[x] Output schemas work                   [x] Email Finder works
[x] Job engine works                      [x] Google Maps/provider architecture works
[x] Queue works                           [x] Business Directory architecture works
[x] Worker works                          [x] Universal scraper foundation works
[x] Retry works                           [x] Scraper cards work
[x] Cancellation works                    [x] Scraper detail pages work
[x] Pause/resume works where supported    [x] Job list works
[x] Checkpoint works                      [x] Job detail works
[x] Result pipeline works                 [x] Progress works
[x] Logs work                             [x] Result page works
[x] Export handoff works                  [x] RBAC works
[x] SSRF protection works                 [x] Private IP blocking works
[x] Resource limits work                  [x] Tests pass (196)
[x] Existing Phase 2 tests still pass     [x] No production data deleted
[x] No secrets committed
```

# PHASE 3 STATUS: PASS
