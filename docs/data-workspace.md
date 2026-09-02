# Data Workspace

Phase 4 overview: how scraped and imported data becomes a workable lead
database, and how the pieces fit together.

## Architecture

```
Scrapers (Phase 3)          Imports (CSV/XLSX/JSON/JSONL)
      ↓                              ↓
  ResultPipeline              LeadImportService
      ↓                              ↓
      └──────────► LeadIngestionService ◄──────────┘
                        ↓
          normalize → dedup → Lead create/update
                        ↓
        LeadService (the ONLY writer of lead rows)
                        ↓
    tags · notes · activity · quality score · provenance
                        ↓
                   LEAD DATABASE (PostgreSQL)
                        ↓
   ┌────────────────────┼────────────────────┐
 search              filters              saved views
 bulk actions        duplicates/merge      quality dashboard
                        ↓
              IMPORT ⇄ EXPORT (CSV/XLSX/JSON/JSONL, streamed)
                        ↓
          StorageService (local, path-safe) + PostgreSQL metadata
                        ↓
                 Redis / worker process
```

## Centralization rules

- Actors never touch lead tables. The Phase 3 `ResultPipeline` delegates each
  item to `LeadIngestionService`, which adds provenance, quality, relational
  tags and the activity trail, then writes through `LeadService`.
- Manual/API/import writes go through the same normalization
  (`normalize_lead_payload` reusing the Phase 3 key normalizers) so scraped,
  imported and hand-entered leads share ONE vocabulary.
- Scrape jobs report: found / saved (new) / **updated** / duplicates / failed —
  the Phase 4 `records_updated` counter tracks merged re-seens (§26).

## Duplicate handling ladder

| Confidence | Signal | Automatic action | Human action |
|---|---|---|---|
| EXACT | email_norm / phone_norm / source_id equal | flagged, import review candidate | merge / keep both / ignore |
| HIGH | website host equal | flagged | merge / keep both / ignore |
| MEDIUM | name+city (name_key), name+phone | import skip/update/review | merge / keep both / ignore |
| LOW | fuzzy name (ratio ≥ 0.75 or containment) | recorded for review ONLY | human decision |

Nothing merges automatically. Merges fill the primary's EMPTY fields, keep
conflicting values on record (`LeadMergeHistory.conflicts`), re-home
notes/activities/tags, and soft-retire the duplicate (`merged_into_id`).
Merge evidence is never hard-deleted.

## Performance

- Indexes on all filter/sort/dedup columns (see migration 0003): email_norm,
  phone_norm, website_norm, name_key, status+created_at, source, source_type,
  city, state, country, quality_score, created_at, updated_at, import_batch,
  merged_into, business_name.
- Search escapes wildcards; filters/sorts validated against whitelists —
  no user input reaches SQL raw.
- Pagination server-capped (`page_size ≤ 500`).
- Bulk actions are set-based (`UPDATE ... WHERE id IN (...)`), with bulk
  activity rows — no per-lead queries.
- Imports stream rows; exports stream 500-row chunks to disk spool then
  StorageService; large jobs run in the worker process, never in request
  threads.
- The synthetic-scale test (`tests/leads/test_performance.py`) verifies
  search/pagination at thousands of leads, chunked large exports, and that
  bulk actions stay set-based.

## Observability

Structured logs (request-id bound) for: import started/completed, export
started/completed, lead created/updated, bulk actions, duplicate merges,
validation failures. Never logged: passwords, tokens, keys, cookies.
Every mutating API action also writes the global `audit_logs`.

## Known limitations (honest scope)

- Free-text search uses escaped ILIKE — fine at this scale; a trigram/GIN
  index can be added later without API changes.
- JSON import parses the whole array (format-inherent); JSONL/CSV/XLSX stream.
- UI polling is interval-based (4s) — SSE upgrade comes later, API already
  exposes live counters.
- Merge is not undoable in the UI (history rows allow forensic restore).
