# 21 — Backup & Recovery Architecture

> Covers required architecture doc: **Backup/Recovery Architecture** (Brief §32)

## 1. What Is Backed Up

| Asset | Method | Target |
|---|---|---|
| PostgreSQL | Nightly `pg_dump` (custom format, compressed) + weekly basebackup when volume grows | `/qbit-data/backups/db/` |
| Configuration | `.env` (redacted template) + `system_settings` dump + compose file | `/qbit-data/backups/config/` |
| Files/exports | Nightly incremental `rsync --link-dest` snapshot of `exports/ imports/ scraper-results/ attachments/` | `/qbit-data/backups/files/` |
| Vault | Encrypted credential store is inside DB dump; **vault master key** runbook instructs secure off-host copy (never stored beside backups) | operator-managed |

## 2. Schedule, Retention, Verification

| Policy | Value (configurable in `/settings/storage`) |
|---|---|
| Scheduled backups | Daily 03:30 (beat task) + manual trigger button |
| Retention | Daily ×14, weekly ×8, monthly ×6 — **never deletes the newest valid backup; never auto-deletes user data** (Brief §32) |
| Verification | Post-dump restore check into a scratch database (row counts + checksum) — a backup that hasn't been restore-tested is marked UNVERIFIED |
| Integrity | sha256 per artifact; manifest file per run |
| Encryption | Artifacts encrypted (age/GPG) before any off-host copy |

## 3. Restore Procedures (runbook summary)

1. **DB restore:** stop workers → `pg_restore` into fresh DB → run `alembic current`
   check → restart → smoke test (`/api/v1/dashboard/health`).
2. **File restore:** rsync snapshot back to `QBIT_DATA_DIR` → checksum verify →
   reconcile `files` table rows.
3. **Full host loss:** provision new host → compose up → restore DB → restore files →
   restore env (from secure copy) → verify vault (decrypt one secret) → repoint DNS.

## 4. Targets

| Metric | Target |
|---|---|
| RPO | ≤ 24 h (nightly); ≤ 5 min achievable via WAL archiving if operator enables it |
| RTO | ≤ 1 h on same host; ≤ 4 h full host rebuild |
| Backup verification | Weekly automated + after every major version upgrade |

## 5. Safety Rules

- Backups run as background jobs; failure raises `SYSTEM_HEALTH_WARNING` and shows on
  the dashboard — a silent backup failure is treated as no backup.
- Restore is an explicitly authorized action (SUPER_ADMIN, step-up auth, audited).
- The retention job is the **only** automated deleter of backup artifacts and only
  touches its own manifest-managed files.
