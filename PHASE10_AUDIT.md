# PHASE 10 AUDIT — Analytics & Reporting Engine (STEP 0)

Audit date: 2026-09-07 · Base commit: `fc5373f` (Phase 9, v0.9.0, 640/640 tests)
Scope: full repository inspection BEFORE any Phase 10 change (spec §1).
Verdict: **Phase 10 is implementable on the existing foundation. Nothing needs to be rebuilt; everything is additive.**

---

## 1. Architecture as found

| Layer | Technology / location |
|---|---|
| API | FastAPI app factory (`app/main.py`), routers registered per module under `/api/v1` |
| UI | **Server-rendered Jinja2** (`app/templates/*`, `app/ui/*.py`), cookie-session auth, dark theme `app/static/css/qbit.css` |
| DB | PostgreSQL 16 (prod) / SQLite aiosqlite (tests), async SQLAlchemy 2.0, `Uuid` PKs, `timestamp_columns()` (created_at/updated_at, TZ-aware), `PortableJSON` |
| Migrations | Alembic `0001`–`0008`, strictly additive-only, permission seeds inside migrations + `test_migrations.py` table whitelist |
| Workers | `app/worker.py` `ScrapeWorker` with isolated async loops: scrape queue, data jobs, campaigns, inbox outbox, automation |
| Redis | Optional (`RedisManager`); queues degrade to in-process fallback; app stays functional without it |
| RBAC | `app/services/rbac.py` catalog + role matrix; enforcement via `require_permission(code)` API dep + `require_ui_permission(code)` UI dep |
| Audit | `AuditService.log()` — append-only, redacted metadata, never breaks business flow |
| Export | `ExportService` (CSV/JSON/XLSX) → `FileService` → local `StorageService` (`files` metadata table) |
| Tests | pytest + pytest-asyncio, per-test isolated SQLite + `create_app()` DI, `admin_headers`/`viewer_headers` fixtures |

## 2. Operational data available for analytics (verified against models)

| Domain | Tables | Key fields verified |
|---|---|---|
| Scraping | `scrape_jobs`, `scrape_job_events` | status enum, `actor_id`, `actor_version`, `records_found/saved/updated/duplicate/failed`, `started_at/completed_at`, `error_code`, created_at index |
| Leads | `leads` | `status` (NEW→CONVERTED/LOST/ARCHIVED), `source`, `source_type`, `source_actor_id/version`, `source_job_id`, `city/state/country/category/industry`, `tags` JSON mirror, `quality_score`, `created_at` + 17 indexes incl. `ix_leads_status_created` |
| Lead activity | `lead_activities`, `lead_tag_assignments` | event trail, user attribution, created_at index |
| Campaigns | `campaigns`, `campaign_recipients`, `campaign_events`, `campaign_queue` | immutable event stream (`MESSAGE_SENT/DELIVERED/READ/REPLIED/FAILED/BOUNCED/COMPLAINED/OPENED/CLICKED/UNSUBSCRIBED`), recipient timestamp lifecycle (`sent_at/delivered_at/read_at/replied_at/failed_at/opened_at/clicked_at/bounced_at/complained_at`), channel + sending account |
| WhatsApp/Email | `messages`, `conversations`, `provider_events` | direction/status lifecycle timestamps (`sent_at/delivered_at/read_at/failed_at`), per-account linkage |
| Inbox | `conversations`, `conversation_events`, `conversation_notes` | status/priority/assignment, `last_inbound_at/last_outbound_at/closed_at`, `unread_count` |
| Automation | `workflow_executions`, `workflow_execution_steps` | status, `entity_type/id`, `error/error_class`, `started_at/completed_at`, created_at index |
| Users/team | `users`, `user_roles` | assignment via `conversations.assigned_user_id`; **no team table exists** (reserved `assigned_team_id` — documented limitation, mirrors Phase 8 §50) |
| Opt-outs | `opt_out_records`, `suppression_entries` | channel, reason, source, created_at |

## 3. Existing analytics surfaces (REUSED, not duplicated)

- `services/marketing/analytics.py` — per-campaign + email campaign rates with honest zero-denominator handling → Phase 10 campaign detail reuses these definitions.
- `automation/services/analytics.py` — `workflow_stats()`, `global_counters()` → reused for the automation domain and `/analytics/automation`.
- `services/inbox/workspace.py` — counters (server-side, visibility-scoped) → pattern for inbox analytics scoping.
- Scrape job counters live on `ScrapeJob` rows; job events give timeline data.

## 4. Gaps Phase 10 must fill (all additive)

1. No cross-domain dashboard; marketing/automation analytics are per-entity only.
2. No saved reports / runs / snapshots; no report builder persistence.
3. No daily aggregate tables, no aggregation worker, no reconciliation diagnostics.
4. No analytics RBAC (`analytics.*`, `reports.*`), no analytics caching, no analytics APIs.
5. Missing analytics-friendly indexes: `conversations.created_at`, `messages.created_at`, `campaign_recipients.created_at`, `workflow_execution_steps` action stats, `lead_activities.created_at`.
6. No SVG chart rendering in the UI (pages are server-rendered; charts must be dependency-free, matching the project's no-framework rule).

## 5. Constraints extracted from prior phases (binding for Phase 10)

- Additive-only migrations; SQLite-test-portable schema (`PortableJSON`, no PG-only DDL).
- Permission matrix must be seeded both in `services/rbac.py` AND in the migration (existing deployments upgrade via Alembic, not reseed).
- `test_migrations.py::test_greenfield_repo_had_no_preexisting_schema` asserts the exact table set → new tables must be registered in a new whitelist constant.
- Envelope `{"success": True, "data": ...}`; errors via `app.core.errors` (`NotFoundError`, `ValidationError`, `PermissionDeniedError`).
- No fabricated metrics anywhere (Phase 5/7/8 precedent: rates return 0.0/None-safe; unavailable provider metrics shown as "not available").
- Redis is optional → caching must be a best-effort layer with direct-query fallback.
- Worker loops must be failure-isolated (one loop crashing never touches the others).

## 6. Plan (implemented in this phase)

`app/analytics/` package: `core/` (time, filters, cache, exceptions), `domains/` (overview, leads, scraping, marketing incl. whatsapp/email, inbox, automation, team), `aggregation.py` (daily aggregate refresh/rebuild), `reports/` (service + worker + exporters). New models in `app/models/analytics.py`. APIs under `/api/v1/analytics/*` + `/api/v1/reports*`. UI under `/analytics*` + `/reports*` with a dependency-free SVG chart helper. Worker gains `_analytics_loop` (aggregation refresh + report runs). Migration `0009_analytics_reporting` (tables + indexes + permissions). Tests in `tests/analytics/`. Docs: `docs/analytics.md`, `docs/report-builder.md`, `docs/metric-definitions.md`.
