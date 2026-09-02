# 23 — Scaling, Cost Optimization & Failure Recovery

> Covers required architecture docs: **Scaling Strategy (22)**, **Cost Optimization
> Strategy (23)**, **Failure/Recovery Strategy (24)** · All 17 failure scenarios from
> Brief §39 with Detection / Recovery / Retry / User visibility / Data safety

## 1. Scaling Strategy (Brief §40: 10k → 100k → 1M+ leads)

| Scale | Bottlenecks | Countermeasures |
|---|---|---|
| 10,000 leads | None | Baseline: indexes of doc 06 §5, single workers |
| 100,000 leads | List queries, dedup lookups, queue bursts | Covering indexes + keyset pagination on lead tables; batch ingestion (COPY); per-source queue concurrency; dashboard rollup tables |
| 1,000,000+ leads | Table size, event volume, export memory | Monthly partitioning on `messages`/`message_events`/`event_log` (BRIN time indexes); trigram-only fuzzy search with `pg_trgm` limits; export streaming (constant memory); scraper workers scale horizontally; optional read replica (still self-hosted); lead search moved to dedicated GIN/trigram strategy |

Multiple concurrent scraping jobs, campaigns, and team accounts are supported by
queue-per-concern routing (doc 09) and per-connection rate budgets (docs 11–12) —
scaling is adding workers/accounts, not redesign. Nothing here is premature: the
partitioning and rollups are config-triggered decisions documented now and applied
when thresholds are hit.

## 2. Cost Optimization Strategy (Brief §33)

| Principle | Application |
|---|---|
| Self-hosting | Single VPS footprint; Docker Compose; no managed cloud needed |
| Open-source libraries | FastAPI, SQLAlchemy, Celery, Scrapy/Playwright, Tailwind — zero license cost |
| Local storage | Filesystem first; cloud only optional connectors |
| No mandatory SaaS | WhatsApp via official API only (required by policy), email via plain SMTP default; SES/Graph optional |
| No license machinery | No activation server, no subscription validation (Brief §34) |
| Efficient workers | Queue-based elasticity; stop idle workers; beat consolidates periodic work |
| Paid APIs | Only where unavoidable or explicitly admin-selected; every adapter documents its cost surface |

## 3. Failure/Recovery Matrix (Brief §39 — all 17 scenarios)

| # | Scenario | Detection | Recovery | Retry | User visibility | Data safety |
|---|---|---|---|---|---|---|
| 1 | Worker crash | Missing heartbeat lease | Lease expiry → job back to QUEUED | Resume from checkpoint | Job shows "resumed" | Checkpointed, no loss |
| 2 | Redis unavailable | `readyz` Redis ping fails, enqueue errors | Jobs remain QUEUED in DB; recovery beat task re-enqueues on reconnect | Re-enqueue once | Health widget red; jobs show queued | Intent persisted in Postgres |
| 3 | PostgreSQL unavailable | `readyz` fails; 5xx envelope | App returns 503 w/ envelope; workers pause loops | Automatic reconnect + backoff | Clear system-health banner | Nothing accepted = nothing lost |
| 4 | Network failure | Timeouts/DNS errors in workers | Circuit-breaker per source/provider | Exponential backoff + jitter | Job/campaign counters show errors | Idempotent tasks |
| 5 | Provider API failure | Non-2xx/webhook gap | Campaign auto-pauses after threshold; connection DEGRADED | Bounded retries, then dead-letter | Campaign status + reason shown | Recipient rows persist verdicts |
| 6 | Invalid credentials | `verify_connection()` fail at TEST/send | NEEDS_REAUTH; campaigns auto-paused | N/A (operator action) | Connection card + banner | Secrets re-encryptable via vault |
| 7 | Expired token | 401 from provider; scheduled probe | Refresh flow where supported; else NEEDS_REAUTH | Refresh once then stop | Same as #6 | No plaintext exposure |
| 8 | Scraper failure | Task exception → event `SCRAPE_JOB_FAILED` | Structured error on job; optional requeue | Max 5 backoff | Job detail error panel | Checkpoint preserved |
| 9 | Duplicate records | Dedup counters rise | Match → enrich metadata, not insert | n/a | Duplicate count on job | Single canonical lead |
| 10 | Large scrape | Row/time thresholds | Chunked checkpoints; pagination cursors | Continue from checkpoint | Progress bar + ETA | Partial results always saved |
| 11 | Large export | Size guard pre-check | Multi-file chunked output or operator confirm | Resume from row cursor | Export status page | Streams; no OOM; metadata row first |
| 12 | Campaign interruption | Dispatcher heartbeat gap / restart | Resume from last unprocessed recipient | Per-message idempotency | Progress + audit trail | No dup sends, no lost recipients |
| 13 | Server restart | Boot sequence | Migrations check → workers re-enqueue orphans | Orphan reconciliation | Brief downtime banner | DB is source of truth |
| 14 | Browser crash | n/a (client) | Session persists server-side; HTMX refetch | User refresh | Nothing lost client-side | All state server-side |
| 15 | Storage full | Health job free-space <10% | Critical → refuse exports/imports; scraping-to-DB continues | After space freed | Storage widget + explicit errors | No silent truncation; tmp staged |
| 16 | Webhook duplication | `external_event_id` dedup | Drop duplicates (count them) | n/a | Transparent | Exactly-once effects |
| 17 | Duplicate message event | Dedup key `(provider, external_id)` | Idempotent handlers | n/a | Transparent | Counters accurate |

Cross-cutting: every retry is bounded and backoff-jittered; every idempotency key is
documented (job_id, message_id, external_event_id); dead-letter queue gives operators
a safe inspection point (doc 09 §6).
