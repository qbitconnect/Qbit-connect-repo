# Import / Export

Phase 4 module. All files flow through the Phase 2 `StorageService` and
`FileService` — clients only ever see file ids, never filesystem paths.

## Import

### Flow

```
Upload → Inspect → Map columns → Preview → Validate → Import options
       → Import (inline or background) → Deduplicate → Complete
       → Downloadable rejected-rows report
```

### Formats

| Format | Parsing | Notes |
|---|---|---|
| CSV | `csv.DictReader` streaming | UTF-8 (BOM tolerated), configurable delimiter |
| XLSX | openpyxl `read_only=True` streaming | sheet selection by index or name, header row configurable |
| JSON | whole-file parse (inherent to format) | must be an array of objects (single object accepted) |
| JSONL | line-by-line streaming | one object per line; malformed line → honest failure |

Row feeds are streamed and processed in chunks; counters are flushed to the
`import_batches` row every 500 rows so progress is real.

### Column mapping

Headers are never assumed. `POST /leads/imports/{id}/mapping` takes
`{csv_column: lead_field}` — fields validated against a whitelist
(`business_name, contact_name, first_name, last_name, email, phone, website,
address, city, state, postal_code, country, category, industry, source_id,
source_url, source, tags`). Unmapped columns are preserved under
`metadata.extra_fields`. A `tags` column is split on `;` or `|`.

### Validation

Every row passes `normalize_lead_payload` (same path as manual/API edits):
whitespace collapsing, length caps, email format, phone (≥7 digits →
`+E.164`-style normalized key), website host check. Invalid values REJECT the
row — they are never silently dropped.

A row without `business_name` and `contact_name` is rejected
("Missing required business name") and lands in the rejected report.

### Duplicate strategies (§25)

| Strategy | Behaviour |
|---|---|
| `SKIP_DUPLICATES` (default) | matches (HIGH/MEDIUM) are skipped, never overwritten |
| `UPDATE_EXISTING` | only FILLS EMPTY fields on the matched lead; non-empty values kept |
| `CREATE_NEW` | no matching, always insert |
| `REVIEW` | insert flagged + create a duplicate-review candidate (never auto-merge) |

Matching uses the Phase 3 pipeline `Deduplicator` on normalized keys
(email/phone/website/name_key).

### Import batches

`import_batches` tracks: filename, file_id, format, status
(`QUEUED→PROCESSING→COMPLETED|PARTIAL|FAILED|CANCELLED`), total/valid/invalid/
imported/duplicate/updated/review rows, bounded error summary, error_file_id,
operator, timestamps, worker lease.

### Rejected-rows report

Invalid rows are written to a CSV (`row, error, data`) capped at 10,000 rows,
stored via StorageService (`IMPORT` category), linked on the batch, and
downloaded with `leads.export` permission. Invalid rows never disappear
silently.

### Background processing

Batches above `QBIT_LEADS_INLINE_IMPORT_MAX_ROWS` (default 5000) stay QUEUED
and are claimed by the worker (`DataJobWorker`, DB-first guarded UPDATE —
same pattern as the scrape queue). Stale leases (crashed worker) are swept to
FAILED with an honest error.

## Export

### Scopes and formats

- Scopes: `selected` (ids), `filtered` (search+filters), `page`, `all`,
  `lead` (single).
- Formats: CSV, XLSX, JSON, JSONL — all streamed in 500-row chunks through
  incremental writers to a spooled temp file, then registered via FileService.
  Nothing is materialized in RAM per dataset.
- Fields: `all` (default), or a validated custom selection from the
  whitelisted export columns.

### History and security

Every export creates a `lead_exports` row (format, scope, filters snapshot,
fields, row_count, status, file_id, operator, timestamps). Downloads resolve
the file id through FileService (path-traversal guarded, permission-checked
`leads.export`). Large exports (above `QBIT_LEADS_INLINE_EXPORT_MAX_ROWS`,
default 20000) are queued for the worker with live row-count progress.

### Progress

```
Exporting... row_count updated per chunk (live counter, never a fake %)
```

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `QBIT_LEADS_INLINE_IMPORT_MAX_ROWS` | 5000 | rows above → worker import |
| `QBIT_LEADS_INLINE_EXPORT_MAX_ROWS` | 20000 | rows above → worker export |
| `QBIT_LEADS_MAX_BULK_IDS` | 5000 | max ids per bulk call |
| `QBIT_MAX_UPLOAD_MB` | existing | upload size cap |

## Troubleshooting

- **Batch stuck in QUEUED** — the worker process (`python -m app.worker`)
  is not running or cannot reach the DB; large imports are processed there.
- **Batch FAILED "worker lease expired"** — the worker crashed mid-run;
  re-submit the mapping call (the source file is still in storage).
- **Rows rejected with "Invalid email"** — check the rejected report; the row
  data column contains the raw values for correction.
- **Duplicate counts higher than expected** — matching uses normalized keys;
  `+91 98765 43210` and `9876543210` are the same lead. Use `CREATE_NEW` only
  when you truly want separate records.
