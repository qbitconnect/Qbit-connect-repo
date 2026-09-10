# 31 — QBIT Actor Platform

The Actor Platform turns QBIT Connect's scraping subsystem into an
**Apify-class internal execution platform** (private, self-hosted,
cost-free-first) while reusing the existing actor contract, job engine and
lead pipeline end-to-end. This document is the operator/engineering reference
for the platform added in migration `0013` and the accompanying code.

---

## 1. Architecture

```
Actor (scrapers/actors/<slug>/)
    ↓  input schema (pydantic, strict pre-validation)
Run (scrape_jobs + outcome + trigger + task_id)
    ↓  claim (atomic guarded UPDATE) → runner
Request Queue (actor_request_queue — persistent, priority, deduped)
    ↓  actor.run(ctx) — async generator, cooperative pause/cancel
Dataset (actor_datasets + actor_dataset_items — every normalized item)
    ↓  normalization → dedup → provenance → leads (existing pipeline)
Exports / API / Webhooks / Schedules / Storage
```

The FastAPI process never executes actors; the worker is the only executor
(claim → execute → finalize). Runs survive restarts via DB checkpoints and
lease-based crash recovery.

## 2. Concepts

| Concept | Storage | Notes |
|---|---|---|
| Actor | code + registry metadata | 12 built-in actors; schema-driven UI |
| Task | `actor_tasks` | saved input+config; run / duplicate / delete |
| Run | `scrape_jobs` | `trigger` = MANUAL/TASK/SCHEDULE/API/RETRY; `outcome` = SUCCEEDED/PARTIAL/TIMED_OUT (derived ONLY from real events) |
| Dataset | `actor_datasets(_items)` | one per run; search/sort/paginate; exports |
| Request queue | `actor_request_queue` | URL-level dedup (`url_norm`), priority, depth, retries |
| KV storage | `actor_kv_entries` | actor state, checkpoint metadata, JSON docs |
| Schedules | `scrape_schedules` | ONCE / INTERVAL / DAILY (worker-fired, trigger=SCHEDULE) |
| Run webhooks | `run_webhooks(_deliveries)` | HMAC-signed, retry w/ exponential backoff (5 attempts) |
| Health history | `actor_health_checks` | registry check + 72h run-history signal |

## 3. Run outcomes (honesty rules, spec §42)

- `SUCCEEDED` — ran to natural completion.
- `PARTIAL` — completed, but stopped early by a configured limit
  (`LIMIT_REACHED` event).
- `TIMED_OUT` — wall-clock budget exhausted; the run is PAUSED at a
  checkpoint and resumable. A resumed run clears the outcome until it is
  re-earned.
- A run that yields nothing because the target served a login/anti-bot wall
  FAILS with `TARGET_BLOCKED` — the reason is visible in run logs. Nothing is
  ever fabricated.

## 4. Built-in actors (12)

| Actor | Category | Highlights |
|---|---|---|
| google-maps | business_leads | existing |
| website | website | existing |
| sitemap-intelligence | website | existing |
| email-finder | email | existing |
| business-directory | directory | existing |
| public-data | public_data | existing |
| universal-web | universal | v2: `strategy=auto` layered extraction (JSON-LD → OG → embedded JSON → contacts → links/tables) + optional headless fallback when Playwright is genuinely available |
| **instagram** | social_media | public profiles/posts/hashtags/search; login walls reported honestly |
| **meta-ads-library** | ads | public Ad Library; per-ad `content_hash` → snapshot change detection |
| **linkedin-public** | social_media | logged-out company/profile surfaces; authwalls never bypassed |
| **justdial** | directory | category+city search; public phone glyph decoding (never guesses) |
| **indiamart** | ecommerce | product/supplier search, price/MOQ hints, tel: contact capture |

Legal/access boundaries (hard): public data only; no login, no CAPTCHA
handling, no evasion, no private data; robots.txt honored by the HTTP layer;
SSRF guard on every hop.

## 5. API surface

Versioned under `/api/v1`, with spec-path aliases at `/api` (no `/v1`):

- `GET /actors` — catalog with per-actor stats (success rate, avg runtime,
  duplicate rate) + latest health
- `GET /actors/{slug}` — input schema, output fields, capabilities, examples
- `POST /actors/{slug}/validate` — strict pre-enqueue validation
- `POST /actors/{slug}/runs` — create + enqueue (spec: `POST /api/actors/{actor}/runs`)
- `GET /runs` · `GET /runs/{id}` · `POST /runs/{id}/pause|resume|cancel|retry`
- `GET /runs/{id}/logs` — structured event stream
- `GET /runs/{id}/dataset`
- `GET /datasets` · `GET /datasets/{id}` · `GET /datasets/{id}/items` (search,
  filter, sort, paginate)
- `POST /datasets/{id}/export` — json / jsonl / csv / xlsx / xml; ALL,
  SELECTED (`ids`) or FILTERED (`search`/`field`+`value`) slices
- `GET /datasets/{id}/changes` — snapshot change detection: new / unchanged /
  modified / stopped / resumed (3-way with `baseline`)
- `GET|POST|PATCH|DELETE /tasks` + `POST /tasks/{id}/run|duplicate`
- `GET|POST|PATCH|DELETE /run-webhooks` + deliveries + `POST …/test`
- `GET|PUT|DELETE /storage/kv/{scope}/{key}` · `GET /storage/queues`

Permissions reuse the `scraping.*` capability set; every endpoint is
audit-logged. Webhook secrets are write-only (revealed once at creation).

### Webhook signatures

`X-QBIT-Signature: t=<unix_ts>,v1=hmac_sha256(secret, ts + "." + body)` —
verify the timestamp window before trusting payloads.

## 6. Cross-process job discovery (Redis-less deployments)

Without Redis the queue backend is in-process per process. The worker's
backend therefore polls `scrape_jobs` for QUEUED rows (bounded, 5 s commit
grace, 30 s per-job re-dispatch suppression) so jobs created by the API
process are picked up without a broker. The runner's atomic guarded claim
keeps multi-worker dispatch safe. Redis deployments use the Redis backend
exactly as before.

## 7. Health monitoring & observability

- Worker loop runs `actor.health_check()` + a 72-hour run-history signal for
  every enabled actor every `QBIT_ACTOR_HEALTH_INTERVAL_SECONDS` (default
  300 s, 0 disables) and persists rows to `actor_health_checks`.
- Status derivation (worst signal wins): registry READY/DEGRADED/FAILED +
  run history HEALTHY / DEGRADED (some failures) / FAILING (all failed) /
  UNKNOWN (no finished runs in the window).
- `ActorStats` computes success rate, average runtime, items saved and
  duplicate rate from REAL `scrape_jobs` rows — surfaced in `/actors` and
  `GET /api/v1/actors`.

## 8. Configuration additions

| Setting | Default | Meaning |
|---|---|---|
| `QBIT_ACTOR_HEALTH_INTERVAL_SECONDS` | 300 | health monitor cadence (0=off) |
| `QBIT_RUN_WEBHOOK_POLL_SECONDS` | 5.0 | webhook delivery poll cadence |
| `QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS` | false | loopback/private SSRF guard override (dev only) |

## 9. UI (internal Apify-style console)

- `/actors` — schema-driven catalog: search, category tabs, per-actor stats +
  health, links into the existing workbench (`/scraping/{slug}`)
- `/datasets`, `/datasets/{id}` — dataset browser with search, paging and
  one-click CSV/JSON/JSONL/XLSX export
- `/tasks` — saved configurations (create/run/duplicate/delete)
- `/run-webhooks` (+ detail) — subscriptions and the delivery log
- `/storage` — KV entries + request-queue stats
- `/api-docs` — REST reference with quickstart

## 10. Safety & data guarantees

- Migration `0013` is additive-only (8 new tables + 4 nullable columns);
  verified up/down on SQLite and covered by `tests/test_migrations.py`.
- Dataset items are JSON-sanitized exactly like the JSONL writers
  (`datetime`/`UUID` → strings); no second data format.
- Every run's dataset `clean_status` records whether it is complete or
  partial — exports of partial datasets are labeled by the UI.
- Formula-injection neutralization for CSV exports mirrors the lead exporter.

## 11. Testing

- `tests/scrapers/test_new_actors_parsers.py` — deterministic fixture-based
  parser tests for all five new actors + extraction toolkit + universal auto.
- `tests/scrapers/test_new_actors_runs.py` — run-level tests via MockTransport
  incl. honest block-failure assertions.
- `tests/test_actor_platform_api.py` — full API/E2E: run → worker → dataset →
  export chain on a local fixture HTTP server; task lifecycle (acceptance
  TEST 10); webhook delivery honesty; change detection; spec aliases.
- `tests/test_actor_platform_ui.py` — console pages render + anonymous guard.
- Live smoke: `scripts/actor_platform_smoke.py` (35 checks against the real
  app: real worker run on the app's own login page, dataset, exports, tasks,
  webhooks, storage, UI pages, aliases).
