# 22 — Observability Architecture

> Covers required architecture doc: **Observability Architecture** (Brief §31)

## 1. Structured, Correlated Logging

- **structlog** with JSON output (`QBIT_LOG_FORMAT=json`) in production; human-readable
  dev console format for local runs.
- Every log line binds: `request_id`, `user_id` (when authenticated), and — as context
  deepens — `job_id`, `campaign_id`, `message_id`, `connection_id`, `scraper_id`.
- Middleware generates/propagates `X-Request-ID`; workers inherit it from the job row
  so an operation is traceable across HTTP → queue → worker (Brief §31 chain:
  Request ID → Job ID → Lead ID → Campaign ID → Message ID).
- Log redaction processor strips credential-like fields before emission (doc 17 §5).
- Rotation: `logs/` rotating files, N days configurable; log volume is a health signal.

## 2. Health & Readiness

| Endpoint | Checks |
|---|---|
| `GET /healthz` (liveness) | Process up |
| `GET /readyz` (readiness) | DB roundtrip, Redis ping, storage writable, migration version current |
| Worker heartbeat | Each worker updates Redis heartbeat key; beat task alerts on staleness (doc 09) |

`/settings/system` + dashboard "System Health" widget render: database health, queue
depth per concern, worker heartbeats, storage free space, last successful backup —
the exact set the brief demands (worker/database/queue/storage health).

## 3. Metrics (self-hosted friendly)

- App exposes lightweight Prometheus-compatible `/metrics` (optional scrape).
- Baseline counters/gauges: jobs by state, queue depths, send success/failure per
  channel, scrape records/s, export durations, DB pool saturation, event relay lag.
- No external SaaS required; operators may scrape with any collector or none — the
  dashboard works from DB/event rollups regardless (cost rule, Brief §33).

## 4. Error Tracking

- All unhandled exceptions: structured log + user-facing error envelope with
  `request_id` (never a raw stack trace to the UI).
- Optional self-hosted Sentry-compatible sink via env var — disabled by default so no
  data leaves the host without explicit admin action (data ownership, Brief §42).

## 5. Tracing an Incident (worked example)

1. Operator reports "WhatsApp campaign stalled".
2. Filter logs by `campaign_id` → see dispatcher heartbeat stop at 08:14:59.
3. Same `request_id` chain → connection event `CONNECTION_DEGRADED` at 08:14:55.
4. `event_log` query → last `MESSAGE_SENT` vs `MESSAGE_QUEUED` gap → provider
   timeouts in worker logs with the same `job_id`.
5. Resolution recorded on the connection timeline; campaign resumed; all state was
   durable, no message double-sent (idempotency keys).

Every important operation is traceable by construction — correlation ids are
mandatory fields in the event envelope (doc 16 §2) and in every logger binding.
