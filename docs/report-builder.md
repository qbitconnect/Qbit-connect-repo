# Report Builder (Phase 10 spec §17–§19)

Saved reports persist a validated metric/dimension/filter configuration and
execute it in the background, snapshotting results for reproducible exports.

## Configuration

A report stores an **allowlisted** configuration (no raw SQL, no expressions,
unknown keys rejected):

```json
{
  "domain": "LEADS",            // OVERVIEW|LEADS|SCRAPING|MARKETING|WHATSAPP|EMAIL|INBOX|AUTOMATION|TEAM
  "metrics": ["total", "converted"],
  "dimensions": ["source"],     // domain-specific; max 3
  "filters": {"city": ["Pune"]},
  "period": "30d",              // presets or "custom" (+ date_from/date_to)
  "grouping": "day",
  "sort_by": "total",
  "sort_dir": "desc",
  "visualization": "table",     // kpi|table|line|area|bar|funnel|donut
  "limit": 100                  // 1–1000
}
```

Available metrics/dimensions per domain are enforced by
`app/analytics/reports/schemas.py` (single source of truth, also drives the
UI builder form). Funnel visualization is LEADS-only; a domain without a day
series honestly FAILS a line report instead of fabricating one.

## Row kinds

- **kpi** — one row, selected metrics from the domain KPI dict.
- **dimension** — rows per dimension value (source rows carry the full
  source-performance metrics; other distributions provide counts, and
  unavailable per-dimension metrics are reported as `null` with a note).
- **timeseries** (line/area/bar + grouping=day) — daily series.
- **funnel** (LEADS) — stage/count/share/step-conversion rows.

## Lifecycle

```
Report (ACTIVE/ARCHIVED)
  └─ ReportRun (QUEUED → RUNNING → COMPLETED | FAILED | CANCELLED)
       └─ ReportSnapshot (immutable result, row_count, export_file_id?)
```

- `POST /api/v1/reports/{id}/run` queues a run with a **frozen config
  snapshot + stored timezone** — editing the report afterwards never changes
  an already-queued run (reproducibility, spec §19).
- The worker (`app/analytics/reports/executor.py::ReportWorker`) claims
  QUEUED runs (plus stale-lease recovery, 10 min) each cycle; failures are
  recorded on the run row with the honest error text.
- Snapshots are bounded: beyond `QBIT_ANALYTICS_MAX_SNAPSHOT_ROWS` the rows
  spill to an EXPORT-category file via StorageService and the snapshot
  references it (`export_file_id`).

## APIs (spec §24)

```
GET    /api/v1/reports                     list (visibility-filtered)
POST   /api/v1/reports                     create (reports.create)
GET    /api/v1/reports/{id}                detail + recent runs
PUT    /api/v1/reports/{id}                edit (owner or reports.manage)
DELETE /api/v1/reports/{id}                delete (owner or reports.manage)
POST   /api/v1/reports/{id}/run            queue execution (202)
POST   /api/v1/reports/{id}/duplicate      copy (starts PRIVATE)
POST   /api/v1/reports/{id}/archive        archive
POST   /api/v1/reports/{id}/restore        restore
GET    /api/v1/reports/{id}/runs           run history (paginated)
GET    /api/v1/reports/{id}/runs/{run_id}  run + snapshot payload
GET    /api/v1/reports/{id}/export?format=csv|xlsx|json[&run_id=…]  export
```

Exports reuse the existing ExportService → FileService pipeline; the export
action is audit-logged. POST /api/v1/reports/preview (UI session) computes
rows for the builder without persisting anything.

## UI (spec §30–§31)

- `/reports` — card list + inline builder.
- `/reports/new`, `/reports/{id}/edit` — builder form (domain-aware
  metric/dimension toggles, dependency-free).
- `/reports/{id}` — config preview, latest snapshot table, run history,
  per-run CSV export, archive/restore/duplicate/delete (owner-managed).

## Visibility & ownership (spec §26, §32)

- PRIVATE: owner + `reports.manage` holders (foreign reports 404 — no
  existence leak).
- TEAM: any `reports.view` holder (no team table exists yet — same posture
  as the Phase 8 workspace).
- GLOBAL: everyone with `reports.view`.
- Mutations require ownership or `reports.manage`; run requires
  `reports.run`; export requires `reports.export` — all enforced
  server-side and audit-logged (created/edited/duplicated/archived/
  executed/exported, spec §27).
