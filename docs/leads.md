# Leads — Data Workspace

Phase 4 module. Everything on this page is backed by real database queries;
the UI never displays invented numbers.

## Lead schema

Canonical table: `leads` (model `app/models/scrape.py::Lead`).

| Group | Fields |
|---|---|
| Identity | `id` (uuid), `business_name`, `contact_name`, `first_name`, `last_name` |
| Contact | `email` / `email_norm`, `phone` / `phone_norm`, `website` / `website_norm` |
| Location | `address`, `city`, `state`, `postal_code`, `country` |
| Classification | `category`, `industry`, `rating`, `review_count`, `social_links` (JSON) |
| Provenance | `source`, `source_type` (`scraper|import|manual|api`), `source_id`, `source_url`, `source_actor_id`, `source_actor_version`, `source_job_id`, `imported_file_id`, `import_batch_id`, `scraped_at` |
| Workflow | `status` (see below), `quality_score` (0–100), `archived_at`, `merged_into_id`, `last_verified_at` |
| Lifecycle | `first_seen_at`, `last_seen_at`, `seen_count`, `created_at`, `updated_at`, `created_by` |
| Extras | `metadata_json` (JSONB — scraper-specific fields like `rating`, `opening_hours`, `extra_fields`), `tags` (JSON mirror of the relational assignments) |

The `*_norm` columns are the dedup/search keys (indexed). Never put
scraper-specific columns on `leads` — they belong in `metadata_json`.

### Statuses

Defaults (enum `LeadStatus`): `NEW, VERIFIED, QUALIFIED, CONTACTED, REPLIED,
INTERESTED, NOT_INTERESTED, CONVERTED, LOST, ARCHIVED`. Custom uppercase codes
(`^[A-Z][A-Z0-9_]{1,29}$`) are accepted so the vocabulary stays configurable.
`ARCHIVED` is excluded from default listings; `RESTORE` returns a lead to `NEW`.
Merged leads keep their row (soft-retired) with `merged_into_id` set and are
excluded from default listings — history is never hard-deleted.

### Quality score (deterministic — NOT an AI prediction)

`+20 business_name +20 phone +20 email +15 website +10 address +5 city
+5 state +5 source provenance = 100`. Computed by
`app/services/leads/quality.py`; recompute via `POST /api/v1/leads/quality/recompute`.

## Tags / Notes / Activity

- `lead_tags` + `lead_tag_assignments` are the relational source of truth
  (unique per pair). The `leads.tags` JSON column is a display mirror kept in
  sync by `TagService`.
- `lead_notes` — multiple notes per lead, author tracked (`user_id`).
- `lead_activities` — per-lead trail: created, scraped, imported, updated,
  status changed, tag added/removed, note added, merged, archived, restored,
  exported. Separate from the global `audit_logs` but complementary.

## Provenance

"Where did this lead come from?" is always answerable:

```
Google Maps → scraper google-maps v1.0.0 → scrape job #id
            → lead.source_actor_id / source_actor_version / source_job_id
CSV import  → files(id) → import_batches(id)
            → lead.imported_file_id / import_batch_id
```

Normalization never drops provenance (see `LeadIngestionService`).

## Search / Filter / Sort

- Free-text search: escaped ILIKE across business, contact, phone, email,
  website, city, state, country, category, source.
- Advanced filters: JSON groups `{"and":[{"field","op","value"}, {"or":[...]}]}`
  validated server-side against a whitelist (fields × operators × types).
  Operators: `eq neq contains starts_with ends_with empty not_empty gt gte lt
  lte between`. Virtual fields: `has_phone has_email has_website has_address tag`.
- Sorting: `sort=-quality_score,city` against a whitelist of 13 columns.
- Pagination: `page`/`page_size` (server-capped), envelope
  `{items,total,page,page_size,total_pages}`.

## API

All endpoints under `/api/v1`, permission enforced server-side (RBAC §32):

```
GET    /leads                          list (page,page_size,search,filters,sort,view_id,include_archived)
POST   /leads                          create (leads.create)
GET    /leads/{id}                     detail incl. notes
PATCH  /leads/{id}                     edit (leads.edit)
POST   /leads/{id}/status              change status (leads.edit)
POST   /leads/{id}/archive             archive (leads.archive)
POST   /leads/{id}/restore             restore (leads.archive)
POST   /leads/{id}/tags                assign tags (leads.edit)
DELETE /leads/{id}/tags/{tag_id}       remove tag (leads.edit)
POST   /leads/{id}/notes               add note (leads.edit)
GET    /leads/{id}/activity            activity trail
POST   /leads/bulk                     bulk actions (per-action permission)
GET    /leads/duplicates               review queue (status filter)
POST   /leads/duplicates/scan          run a scan (leads.manage_quality)
POST   /leads/duplicates/{id}/merge    merge (leads.merge)
POST   /leads/duplicates/{id}/resolve  keep_both / ignore (leads.merge)
GET    /leads/tags / POST/PATCH/DELETE (leads.manage_tags for writes)
GET    /leads/views / POST/PATCH/DELETE (leads.manage_views for writes)
GET    /leads/quality                  quality aggregates
POST   /leads/quality/recompute        recompute scores (leads.manage_quality)
POST   /leads/import                   upload file (leads.import)
POST   /leads/imports/{id}/mapping     map columns + start (leads.import)
GET    /leads/imports                  batch history
GET    /leads/imports/{id}             batch detail/counters
GET    /leads/imports/{id}/rejected    download rejected-rows CSV (leads.export)
GET    /leads/exports                  export history
POST   /leads/export                   create export (leads.export)
GET    /leads/exports/{id}/download    secure file-id download (leads.export)
```

Bulk actions: `add_tag remove_tag set_status archive restore delete export`.
Delete is soft (→ ARCHIVED) unless `params.hard=true`, which additionally
requires `leads.delete`, `params.confirm="DELETE"`, and is capped at 1000 ids
per call. Bulk operations use set-based SQL — never one query per lead.

## Permissions (11)

`leads.view create edit archive delete import export merge manage_tags
manage_views manage_quality`. Role matrix seeded by migration 0003
(SUPER_ADMIN/ADMIN: all; MANAGER/OPERATOR: all except `leads.delete`,
`leads.manage_quality`; VIEWER: `leads.view` only). Frontend hiding is never
the security boundary.

## RBAC matrix summary

| Permission | SUPER_ADMIN | ADMIN | MANAGER | OPERATOR | VIEWER |
|---|---|---|---|---|---|
| leads.view | ✓ | ✓ | ✓ | ✓ | ✓ |
| leads.create/edit/archive/import/export/merge/manage_tags/manage_views | ✓ | ✓ | ✓ | ✓ | — |
| leads.delete | ✓ | ✓ | — | — | — |
| leads.manage_quality | ✓ | ✓ | — | — | — |

## UI

- `/leads` — data workspace: search, filter chips, saved views, column
  visibility, bulk bar, pagination ("Showing x–y of N" from real counts).
- `/leads/{id}` — overview, contact, business, location, source, quality,
  tags, notes, activity, metadata; edit/status/archive/export actions.
- `/leads/import` — wizard (see docs/import-export.md).
- `/leads/duplicates` — side-by-side review with merge / keep both / ignore.
- `/leads/quality` — completeness dashboard (all numbers from queries).
- `/leads/exports` — export history with secure downloads.
