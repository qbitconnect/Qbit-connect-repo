# 30 — Production Release Checklist (Phase 12 §53/§54)

Status: **implemented** · Smoke: `scripts/phase12_smoke.py` (19 checks) ·
Load probe: `scripts/phase12_load_test.py` · Integrity: `python -m app.cli integrity`

## Pre-release gates

| # | Gate | Evidence |
|---|---|---|
| 1 | Full test suite passes | `pytest`: 815 passed, 0 failed (baseline 777 + 34 hardening + 4 worker-reliability) |
| 2 | Security audit pass | `PHASE12_AUDIT.md`: 1 CRITICAL + 8 HIGH fixed; residuals documented |
| 3 | No secrets committed | `git grep` sweep clean (§2); env-manifest backups store NAMES only |
| 4 | Database backup verified | `cli backup` → `cli verify` PASS (pg_restore --list / sqlite integrity_check) |
| 5 | Storage backup verified | files tar.gz `verify` PASS (full listing) |
| 6 | Migrations tested | 0001→0011 up/down cycle on scratch DB + `test_migrations.py` green |
| 7 | Environment variables verified | §4 table; app fails fast when required values missing (tested) |
| 8 | Docker build successful | image builds from `backend/` (pinned python:3.12-slim, non-root) |
| 9 | Frontend build successful | server-rendered UI ships in-image; templates escape-checked |
| 10 | Worker build/start | same image, `app.worker` entrypoint; heartbeat file written |
| 11 | Health endpoints | `/health/live` 200 zero-deps; `/health/ready` aggregate; tested |
| 12 | HTTPS works | nginx conf + certbot; HSTS enabled after verification (§9) |
| 13 | RBAC verified | 127 permissions; permission matrix tests green |
| 14 | Tenant isolation verified | cross-org negative tests (404/403) green |
| 15 | Provider credentials verified | connections validation flows; secrets write-only |
| 16 | Backup restore tested | `cli restore --yes` into ISOLATED SQLite target (interlocks verified) |
| 17 | Monitoring configured | ops snapshot + alert thresholds (docs/29 §16) |

## Post-release smoke (run after every deploy)

`python scripts/phase12_smoke.py` against the live base URL, or manually:

| # | Check | # | Check |
|---|---|---|---|
| 1 | Login works | 9 | Workflow opens |
| 2 | Dashboard loads | 10 | Analytics loads |
| 3 | Lead creation works | 11 | Report list/run works |
| 4 | Lead view works | 12 | Small export downloads |
| 5 | Scraping job works | 13 | Admin console + ops snapshot |
| 6 | Campaign opens (no send) | 14 | Workers healthy (heartbeat fresh) |
| 7 | WhatsApp connection health | 15 | Queue healthy (ops depth) |
| 8 | Email connection health | 16 | DB + storage healthy (health endpoints) |

§54 rule honored: automated smoke NEVER sends real marketing messages.

## Failure-injection drills (§46, non-production)

| Drill | Expected behavior | Verified by |
|---|---|---|
| Worker SIGTERM mid-job | drain grace → cancel → checkpoint + PAUSED (never FAILED) | `test_cancellation_checkpoints_and_pauses` |
| Worker crash (no renewal) | lease expires → sweep re-queues from checkpoint, resumed_count++ | `test_crashed_worker_lease_is_still_recovered` |
| False-positive crash guard | renewed DB lease is NEVER re-queued while running | `test_db_lease_renewal_prevents_false_recovery` |
| Double claim | guarded UPDATE: second claim loses | `test_claim_is_atomic_second_claim_loses` |
| Dequeue broker blip | worker logs + backs off, loop survives | guarded loop (worker.py), M1 |
| Duplicate webhook | unique event id → counted, never double-applied | existing webhook tests + DB unique constraint |
| Webhook missing timestamp | 401 rejected (mandatory replay window) | `test_missing_timestamp_is_rejected` |
| Webhook forged signature | 401 constant-shape rejection | existing HMAC tests |
| Provider timeout / 500 | honest channel error; retryable → backoff; DELIVERY_STATE_UNKNOWN never auto-retried | existing email/whatsapp provider tests |
| Login brute force | per-IP 429 (+Retry-After) then per-account lockout, constant-shape | lockout + limiter tests |
| Restore onto live DB | restore ABORTS without --force (data-safety interlock) | cli restore guard |
| Disk nearly full | ops snapshot `disk.warning=true` at threshold | ops endpoint test |

## Load test results (measured, this machine — §47)

`python scripts/phase12_load_test.py --users 10 --requests 30` (SQLite test DB, 2000 leads):

```
concurrency 10 · 300 requests · 8.04s wall · 37.3 req/s · error rate 0.00%
endpoint              n    p50     p95     p99     max  errors
lead_list            50   61.5  1797.6  4754.9  4754.9       0
lead_search          50   62.0  1368.4  2072.3  2072.3       0
lead_filter          40   56.7   869.9  1577.9  1577.9       0
inbox_load           40   46.6   765.6  1457.1  1457.1       0
dashboard            40   65.7  1179.5  3796.2  3796.2       0
analytics_sources    40   50.6  2267.6  3364.4  3364.4       0
health_ready         40    4.8     8.1    12.1    12.1       0
queue depth 0 · db pool checked-out 0
```

Honest reading: p50 is 47–66 ms across the board; the p95/p99 tail on analytics and
list endpoints reflects SQLite single-writer file-lock serialization under concurrent
load in the TEST harness — production runs PostgreSQL 16, so these tails must NOT be
extrapolated to production. Zero errors, zero leaks, pool returns to 0 (no connection
leak). No capacity rating is claimed without a production-shaped measurement.

## Data integrity audit (§48)

`python -m app.cli integrity` — read-only checks: org-less leads/conversations/
campaigns, messages missing conversations, missing creators, missing teams, duplicate
provider event ids, stale RUNNING leases, files missing on disk. Produces diagnostics
ONLY (never repairs). Result in this phase's final validation: **clean**.
