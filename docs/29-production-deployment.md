# 29 — Production Deployment & Operations Guide (Phase 12)

Status: **implemented** · Version 0.12.0 · Companion: `deploy/README.md` (Docker/Tunnel/VPS
runbooks), `deploy/nginx-qbit.conf` (reverse proxy), `docs/21-backup-recovery-architecture.md`
(policy), `PHASE12_AUDIT.md` (findings fixed), `docs/30-release-checklist.md` (go-live gates).

This is the complete production deployment guide (brief §50) plus the disaster-recovery
plan (§15), rollback strategy (§52), alerting thresholds (§35), retention policy (§29)
and resource limits (§37).

## 1. Server requirements

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU (scraper + marketing workers) |
| RAM | 4 GB | 8 GB |
| Disk | 40 GB SSD | 100 GB SSD (data dir grows with exports/attachments) |
| OS | Ubuntu 22.04/24.04 LTS (any systemd Linux) | |
| Software | Docker Engine ≥ 24 + Compose v2; nginx; certbot | |

## 2. OS setup

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ca-certificates curl gnupg nginx
# Docker (official repo) …
sudo useradd -m -s /bin/bash qbitops && sudo usermod -aG docker qbitops
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
```

## 3. Docker installation

Install Docker Engine + compose plugin from Docker's official apt repository
(see docs.docker.com). Verify: `docker compose version`.

## 4. Environment variables (§3)

Generate `.env` next to `docker-compose.yml` with `python3 deploy/gen-env.py` (never
committed), then set the rest. REQUIRED in production (the app fails fast on missing
values — `config.validate_runtime`):

| Variable | Purpose |
|---|---|
| `QBIT_ENV=production` | enables all fail-fast production checks |
| `QBIT_SECRET_KEY` | ≥32 random chars (gen-env generates one) |
| `POSTGRES_PASSWORD` | DB superuser password (gen-env generates) |
| `REDIS_PASSWORD` | Redis requirepass (gen-env generates) |
| `EMAIL_WEBHOOK_SECRET` | email webhook HMAC secret (Phase 12: required) |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | Meta subscription verification (required) |
| `QBIT_CORS_ORIGINS` | exact origins, comma-separated; wildcard forbidden |
| `DATABASE_URL` / `REDIS_URL` | composed by docker-compose from the above |
| `QBIT_DATA_DIR`, `QBIT_EXPORT_DIR`, `QBIT_LOG_DIR`, `QBIT_BACKUP_DIR` | storage layout (§13) |
| `QBIT_BACKUP_SCHEDULE_HOURS`, `QBIT_BACKUP_RETENTION_*` | backup beat + GFS retention |
| `QBIT_LOGIN_MAX_FAILED_ATTEMPTS`, `QBIT_LOGIN_LOCKOUT_MINUTES` | brute-force lockout |
| `QBIT_WORKER_DRAIN_SECONDS`, `QBIT_DISK_WARN_PERCENT`, `QBIT_COOKIE_SECURE` | ops knobs |

Never hardcode credentials in code, images or logs (§2/§4). Secrets reach the app only
via env/vault; audit + logs redact a 30-name secret-key denylist.

## 5–7. PostgreSQL, Redis, storage

- **PostgreSQL 16** runs in compose with a named volume (`qbit-pgdata`), NO published
  ports, internal network only, healthcheck-gated. Connections pool via SQLAlchemy
  (`QBIT_DB_POOL_SIZE=10` + overflow 20). Migrations are a CONTROLLED step:
  `docker compose exec qbit-api alembic upgrade head` — never run automatically on boot,
  never destructive (§11/§61).
- **Redis 7** runs with `--appendonly yes` + requirepass, internal-only. Redis holds
  queue state, leases, analytics cache and control flags ONLY — the database is the
  source of truth; a Redis loss costs in-flight queue entries that the recovery sweep
  re-enqueues (§16).
- **Storage** is the bind mount `${QBIT_DATA_HOST_PATH}` → `/qbit-data`, persistent,
  holding: `database/ exports/ imports/ scraper-results/ campaigns/ attachments/ misc/
  logs/ backups/ temporary/ cache/` (§13). Backups protect everything except
  `temporary/` and `cache/`.

## 8–9. Reverse proxy + HTTPS

Deploy `deploy/nginx-qbit.conf` (TLS termination, HTTP→HTTPS redirect, proxy headers,
`client_max_body_size 110m`, timeouts, security headers). Certificates via certbot.
HSTS header is pre-written but commented — enable ONLY after HTTPS is verified
end-to-end (§9). TLS never lives inside the application.

## 10. Database migration (§49 dry run)

Procedure (executed for 0011 in this phase — see §49 of the release checklist):

```
backup → alembic upgrade head → app start → smoke (scripts/phase12_smoke.py)
```

All migrations 0001→0011 are additive; downgrade is supported and tested
(test_migrations up/down cycle) but never assumed safe on production — prefer
forward-fix. Restore testing runs in an ISOLATED environment only (§14/§61).

## 11–12. Worker + scheduler startup

`qbit-worker` is the same image with `python -m app.worker`: scrape/data/campaign/
outbox/automation/analytics loops + the periodic recovery sweep + the backup beat
(`QBIT_BACKUP_SCHEDULE_HOURS`, default 24h: DB dump + file archive + config manifest +
verification + GFS prune). `stop_grace_period: 90s` gives `QBIT_WORKER_DRAIN_SECONDS=30`
room to checkpoint-and-pause in-flight jobs on SIGTERM (§18) instead of being SIGKILLed.

## 13. Backup setup (§12/§13)

- Scheduled: worker beat (above). Manual: `python -m app.cli backup [--files]`.
- Artifacts under `QBIT_BACKUP_DIR`: `db/*.dump` (pg_dump custom format), `files/*.tar.gz`,
  `config/env-manifest-*.txt` (variable NAMES only — never values), append-only
  `manifest.jsonl` (sha256 + size per artifact).
- Verification: `python -m app.cli verify <path>` — `pg_restore --list` for dumps,
  full tar listing for archives, SQLite `integrity_check` on a COPY for SQLite.
- Retention: `python -m app.cli prune --yes` (or automatic) — GFS 14 daily / 8 weekly /
  6 monthly, manifest-tracked ONLY; files not recorded in the manifest are never touched.
- Off-host: copy `QBIT_BACKUP_DIR` off the server (rsync to a second machine/object
  storage) and encrypt at rest — recommended, deployment-specific (§12).

## 14. Restore procedure (tested, §14)

```bash
# on an ISOLATED environment with DATABASE_URL pointed at the empty target DB:
python -m app.cli verify <backup>            # confirm the artifact reads back
python -m app.cli restore <backup> --yes     # refuses if target already has QBIT tables
docker compose exec qbit-api alembic upgrade head   # schema is current (dump carries it)
# then: app start → login → leads/campaigns/conversations/files visible → workers resume
```

Verification steps after restore: health endpoints 200, admin can log in, leads list
renders, campaigns/conversations/files open, analytics reconciles from tables,
workers dequeue. The restore command has TWO interlocks: `--yes` required, and it
ABORTS if the target database already contains QBIT tables unless `--force` is also
given (§61: production data is never overwritten silently).

## 15. Disaster recovery plan (RPO/RTO + scenarios)

**RPO**: ≤ 24 h with the default backup beat (set `QBIT_BACKUP_SCHEDULE_HOURS=6` for
RPO 6 h; use streaming/CRON off-host copies for tighter targets).
**RTO**: ≤ 1 h for single-host restore (provision host → restore dump → up).

| # | Scenario | Recovery |
|---|---|---|
| 1 | App crash | compose `restart: unless-stopped` restarts; jobs PAUSED at checkpoints resume |
| 2 | Worker crash | same restart; recovery sweep re-queues expired leases from checkpoints |
| 3 | Redis failure | app degrades to local-first mode / worker backs off with retry; DB-first design loses nothing committed; replace Redis and restart |
| 4 | PostgreSQL failure | restore latest verified dump (§14) + `files/` archive; RPO bound above |
| 5 | Disk failure | new host → restore DB + files + config manifest → re-point DNS |
| 6 | Server failure | same as 5 (off-host backups are the RPO bound) |
| 7 | Corrupted export | re-export from live tables; exports are regenerable |
| 8 | Provider outage | sending fails honestly with channel errors + retries; nothing is faked; resume after provider recovery |
| 9 | Network outage | proxies/timeouts fail requests; workers back off; queue drains on recovery |
| 10 | Accidental config change | `config/env-manifest` names + this guide rebuild `.env`; `git` holds the code; restart |

## 16. Monitoring & alerting foundation (§34/§35)

Endpoints: `/health/live` (liveness, zero deps — Docker HEALTHCHECK), `/health/ready`
(readiness), `/health/database|storage|redis` (components), `/api/v1/admin/ops`
(settings.view-gated): queue depth, scrape jobs by status, worker heartbeat age, disk
usage %, backup manifest summary, uptime.

Alert conditions (poll `/health/ready` + `/admin/ops`; no noisy alerting):

| Condition | Threshold | Action |
|---|---|---|
| readiness 503 | 2 consecutive polls | check `/health/database`, restore DB |
| liveness timeout | 3 failed probes | inspect API logs, restart container |
| worker heartbeat age | > 180 s | inspect worker logs; jobs auto-recover after lease expiry |
| disk used | ≥ `QBIT_DISK_WARN_PERCENT` (85) | prune expired exports, check backups dir growth |
| queue depth | > 500 sustained 15 min | scale workers / inspect failed jobs |
| backup manifest stale | newest entry > 26 h old | run manual backup, check worker beat logs |
| webhook 401 spike | sustained growth | rotated secret on the provider side; update env |

## 17. First admin + provider configuration

`python -m app.cli seed --email you@domain --password '…'` (roles+first SUPER_ADMIN),
then create WhatsApp/Email connections in `/connections` (credentials go to the
encrypted vault, write-only), configure webhooks to `https://<host>/api/v1/webhooks/…`.

## 18–20. Verification, rollback, recovery

Verification = `docs/30-release-checklist.md` (pre/post-release + smoke).
**Rollback strategy (§52)**: keep the previous image tag; `docker compose` down →
retag → up → health + smoke. Database is NOT auto-downgraded: migrations are additive
so the previous image works against the newer schema (forward-compatible by design);
if a migration must be reverted, restore the pre-migration dump instead.
**Recovery** = §14 + §15 above.
