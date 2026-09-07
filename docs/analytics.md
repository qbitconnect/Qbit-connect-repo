# Analytics & Reporting Engine (Phase 10)

The analytics engine provides a unified, permission-aware view of real
operational data across scraping, leads, marketing, WhatsApp, email, inbox,
automation and team activity. Every number originates from stored records or
a documented aggregate — **nothing is fabricated, estimated or demo-seeded**.

## Architecture

```
app/analytics/
├── core/
│   ├── time.py           period presets, wall-clock boundaries, day buckets
│   ├── filters.py        allowlisted filter model (one filter set per request)
│   ├── cache.py          Redis-backed cache (optional, scoped keys)
│   ├── math.py           zero-denominator-safe rates, comparisons
│   ├── query_builder.py  per-table filter appliers (shared by every domain)
│   └── exceptions.py     AnalyticsError hierarchy
├── domains/              one module per reporting area
│   ├── overview.py       cross-domain KPI cards (spec §4)
│   ├── leads.py          KPIs, timeseries, distributions, funnel, sources
│   ├── scraping.py       job/records/version/error analytics
│   ├── marketing.py      campaign KPIs, event stream, channels
│   ├── whatsapp.py       messages/accounts (real provider events only)
│   ├── email.py          sent/delivered/bounce/opens (tracking-gated)
│   ├── inbox.py          conversations, response/resolution times
│   ├── automation.py     workflow executions, failures, step counts
│   └── team.py           per-user attributed activity (visibility-scoped)
├── service.py            AnalyticsService facade (periods, compare, cache)
├── aggregation.py        daily aggregates + rebuild + diagnostics (§20–§21, §29)
└── reports/
    ├── schemas.py        strict allowlisted report configuration
    ├── service.py        CRUD, ownership/visibility, run lifecycle
    ├── executor.py       background run execution → snapshots
    └── exporter.py       CSV/XLSX/JSON via the existing ExportService
```

Read-only rule: analytics never writes to operational tables. The only rows
it creates are its own report/run/snapshot/aggregate metadata.

## Time & timezone rules (spec §23)

- All DB timestamps are timezone-aware UTC (existing convention).
- Period presets (`today`, `yesterday`, `7d`, `30d`, `90d`, `this_month`,
  `previous_month`, `all`, `custom`) resolve **wall-clock boundaries in a
  user-selected IANA timezone**, then compare against UTC instants.
- A report for "September 7" (Asia/Kolkata) covers exactly Sep 7 00:00–23:59
  local time — never Sep 6/7/8.
- Timeseries day labels are produced in SQL: PostgreSQL uses the full tz
  database (`timezone(tz, col)::date`); SQLite (tests/dev) uses a fixed offset
  computed at the range midpoint. UTC is identical on both dialects.
- Future boundaries are clamped to `now` (no empty future buckets).
- Reports store the timezone used; a run freezes it for reproducibility.

## Caching (spec §22)

- Redis-backed, best-effort: with `REDIS_URL` unset everything still works
  via direct queries (local-first rule).
- Keys: `qbit:analytics:v1:{domain}:{scope}:{sha256(filters+period)}` —
  different filters never collide; the **scope** component isolates
  user-scoped analytics (e.g. team pages) so one user can never receive
  another user's restricted numbers.
- TTLs: dashboards 60 s, distributions 120 s (default; configurable via
  `QBIT_ANALYTICS_CACHE_TTL_SECONDS`). TTL-based invalidation only.
- `POST /api/v1/analytics/cache/clear` (admin) drops the cache namespace.

## Daily aggregates (spec §20–§21)

Six aggregate tables (`analytics_daily_leads`, `_campaigns`, `_messages`,
`_conversations`, `_scraping`, `_automation`) store per-(day, dimension)
metrics as derived, **rebuildable** JSON payloads:

- The worker refreshes them incrementally (last aggregated day, −1 day
  overlap, through **yesterday** — today belongs to the live layer) with
  idempotent upserts; interruption is recovered by simply re-running.
- Admin range rebuild: `POST /api/v1/analytics/aggregates/rebuild`
  (`analytics.manage`) with `day_start`/`day_end`/`domains`.
- Every run writes an `analytics_aggregation_runs` bookkeeping row.
- Operational tables remain the source of truth: dashboards and reports
  always compute from live indexed queries, so a stale aggregate can never
  change a displayed number. Aggregates power reconciliation diagnostics
  (`GET /api/v1/analytics/diagnostics` compares aggregate vs live totals and
  flags mismatches instead of auto-fixing them — spec §29).

## Performance (spec §33)

- All series/KPI queries are aggregate GROUP-BYs over indexed, date-bounded
  ranges (no full scans); new analytics indexes ship in migration 0009
  (`conversations.created_at`, `messages.created_at`, `messages(direction,
  created_at)`, `campaign_recipients.created_at`, `lead_activities.created_at`,
  `scrape_jobs.created_at`).
- Pagination bounds every table; report row limits (1–1000) bound snapshots.
- Expensive report runs execute in the background worker with bounded
  snapshot payloads (`QBIT_ANALYTICS_MAX_SNAPSHOT_ROWS`; overflow spills to a
  StorageService export file instead of bloating PostgreSQL).
- Exports stream from disk; no giant in-memory accumulations on the request
  path.

## Security (spec §26, §32)

- Granular server-side permissions (see `rbac.py`): `analytics.view`,
  `analytics.view_leads|scraping|marketing|whatsapp|email|inbox|team|
  automation`, `analytics.manage`, and `reports.view|create|edit|delete|
  run|export|manage`.
- Filters and report configs are strict allowlists — no raw SQL, no
  expression strings, unknown keys rejected with 400/422.
- Foreign PRIVATE reports 404 (no existence leak); visibility PRIVATE/TEAM/
  GLOBAL with owner-or-manage enforcement for mutations.
- Team analytics honours the established visibility rules: ASSIGNED_ONLY
  deployments show only the requesting user's attributed activity.
- Analytics responses never touch the credential vault — no provider
  secrets, tokens or connection credentials can appear in payloads.
- Every mutating action (report CRUD, run, export, rebuild, cache clear) is
  audit-logged with redacted metadata (spec §27).

## Diagnostics (spec §29)

`GET /api/v1/analytics/diagnostics` (admin) checks: negative aggregate
counts, delivered > sent, delivered-without-sent timestamps, negative job
durations, aggregate-vs-live mismatch, orphaned workflow executions,
duplicate provider events. Findings are flagged and logged — **never
auto-fixed**; production data is never modified.
