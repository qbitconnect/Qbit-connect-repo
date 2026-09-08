# PHASE 12 — PRODUCTION READINESS AUDIT

Audit date: 2026-09-09 · Tree: `6affbaa` (post Phase 11 integration) · Scope: full repository
(frontend/UI, backend, database, migrations, Redis, workers, providers, storage, Docker, docs, tests).

Production readiness checklist derived from the Phase 12 brief (§1–§63); findings classified
**CRITICAL / HIGH / MEDIUM / LOW**. CRITICAL and HIGH are fixed in this phase; unresolved items
are listed with severity and are not hidden.

## What already exists (verified, no change needed)

| Area | Status |
|---|---|
| Config fail-fast (`validate_runtime`, §3/§56) | Strong secret key, PostgreSQL required, wildcard-CORS ban, private-target SSRF ban, mock-maps ban, WhatsApp verify token required in production |
| Secret handling (§4) | Redaction denylist on every structured log + audit row; vault credentials write-only; PGPASSWORD via env, never argv |
| Passwords / sessions (§5) | argon2id + rehash-on-login, constant-shape API login failure, server-side `UserSession` revocation, absolute TTL |
| RBAC / tenant isolation (§6) | 127 permissions, `AuthorizationService`, 4 visibility scopes, org guard on `X-Organization-Id` (403 for non-members), 28 enterprise tests incl. cross-tenant |
| Security headers (§7) | XCTO, X-Frame-Options DENY, Referrer-Policy, Permissions-Policy |
| CORS (§8) | Explicit allowlist, credentials-aware, no wildcard in production |
| Scraper SSRF (§22) | `netguard`: scheme/port allowlist, RFC1918/loopback/link-local/metadata blocks, all-DNS-address checks, redirect re-validation, response size caps, robots enforced |
| Webhooks (§21) | HMAC-SHA256 raw-body verification, replay window, DB-unique event-id idempotency, size caps |
| Files (§23) | path-safety module, server-generated keys, dual size enforcement, org+visibility authorization chain, `attachment` disposition |
| Input validation (§24) | Pydantic everywhere, zero raw SQL, zero eval/exec/shell (pg_dump argv-only) |
| XSS (§25) | Jinja2 autoescape, zero `|safe`, nh3 sanitization on email HTML at save+render, sandboxed iframe for inbound HTML |
| Marketing safety (§28) | suppression/unsubscribe/consent gates, channel-honest failure codes, idempotent queue claims |
| Error handling (§31) | typed error envelope with request_id; catch-all 500 hides stack traces |
| Health (§32) | `/health`, `/health/database`, `/health/storage`, `/health/redis` with 200/503 semantics |
| DB hygiene (§11) | additive-only migrations up to 0010, non-destructive downgrades, no startup auto-migrate |
| Docker (§41/§42) | non-root uid 10001, pinned `python:3.12-slim`, internal-only db/redis networks, persistent volumes, no published db/redis ports |

## Findings

### CRITICAL
| ID | Finding | Impact | Fix |
|---|---|---|---|
| C1 | **DB lease is never renewed while a job runs** — `runner._heartbeat` renews only the Redis lease; recovery (`engine.recover_stalled`) judges crashes by the DB `leased_at` column with `QBIT_WORKER_LEASE_SECONDS=120`, while default job wall-clock is 3600s. Every scrape job running >120s is re-queued **while still running** → duplicate concurrent execution (runner.py:424-430, engine.py:283-293) | duplicate pipeline writes, attempt churn, races | heartbeat now also renews the DB lease with a guarded UPDATE (owner+RUNNING predicate) |

### HIGH
| ID | Finding | Impact | Fix |
|---|---|---|---|
| H1 | Scrape job claim is SELECT-then-flush, **not** an atomic guarded UPDATE (runner.py:86-111) — two workers can double-claim one QUEUED row | duplicate execution under scale-out | claim converted to single guarded `UPDATE … WHERE status IN ('QUEUED','PAUSED')` (rowcount-gated) |
| H2 | **No backup scheduling, no file-data backup** — docs/21 promises a daily beat task + file backups; only manual `cli backup` exists, DB only | silent data loss window | worker backup scheduler (`QBIT_BACKUP_SCHEDULE_HOURS`), tar backup of exports/scraper-results/campaigns/attachments, manifest-tracked |
| H3 | `_drain()` never cancels in-flight jobs (worker.py:290-293); Docker's 10s stop timeout SIGKILLs mid-job so the checkpoint+PAUSE shutdown path is unreachable | no graceful shutdown in practice | bounded drain: grace period (`QBIT_WORKER_DRAIN_SECONDS`, default 30) then cancel; compose `stop_grace_period` |
| H4 | **No metrics/ops surface** — no endpoint exposes queue depth, job counts, disk, worker liveness (`pending_count()` is dead code) | blind operations | `/api/v1/admin/ops` snapshot (settings.view-gated): queue depth, jobs by status, disk usage %, worker heartbeat age, backups, uptime |
| H5 | **Stored XSS in job log UI** — `templates/jobs/detail.html:127-131` injects `e.message` (contains attacker-controlled scraped URLs) via `innerHTML` without escaping | script execution in operator browser | escape `e.message`/`e.type` with a JS `esc()` helper (same pattern as inbox) |
| H6 | **Hardcoded fallback webhook secret** — `resolve_secret` returns `"mock-webhook-secret"` for `email_mock` in every env; the route is registered unconditionally | publicly-forgable email webhook in misconfigured production | fallback removed; `email_mock` webhooks rejected in production; `EMAIL_WEBHOOK_SECRET` required in production |
| H7 | **UI login is unthrottled** — `POST /login` form route never touches `login_limiter` (bypasses API limiter); distinct enumeration errors | cookie-session brute force | limiter applied (per-IP), constant-shape errors, Retry-After surfaced |
| H8 | **Rate-limit bypass via X-Forwarded-For spoofing** — `get_client_ip` trusted the FIRST XFF entry (client-controlled), letting an attacker rotate spoofed IPs past the per-IP login limiter when the API is reachable directly | limiter bypass | trust only `X-Real-IP` (set by our proxy) or the LAST XFF entry (appended by our proxy); compose now binds the API to loopback by default (`QBIT_API_BIND_HOST`) |

### MEDIUM
| ID | Finding | Fix |
|---|---|---|
| M1 | Worker main loop dies on transient Redis/DB errors (unguarded `dequeue`); Redis client lacks retry tuning | guarded dequeue with backoff; `retry_on_timeout`, `health_check_interval`, `socket_keepalive` |
| M2 | No liveness/readiness split; API container healthcheck restarts on DB blips; worker liveness invisible | `/health/live` (dep-free) + `/health/ready`; Dockerfile HEALTHCHECK uses `/health/live`; worker heartbeat file checked by compose |
| M3 | Backup: no verification, no retention, no restore tooling, config-dir unused | `verify` (pg_restore --list / tar listing / sqlite integrity), GFS retention pruning honoring the manifest, `cli restore` with --yes guard, config snapshot backup |
| M4 | UI logins bypass session revocation (raw JWT, no `UserSession` row); cookie lacks `secure`; no CSRF second layer | UI login creates a real session row; `_resolve_user` checks revocation; `secure` cookie in production (SameSite=Lax + Bearer API documented as primary CSRF defense) |
| M5 | Email webhook timestamp header optional (`if timestamp_header:`) — replays without the header pass | timestamp now mandatory in `verify_signature` |
| M6 | `SECRET_KEYS` denylist misses `app_secret`/`client_secret`/`verify_token`-style variants | denylist extended (16 new key names) |
| M7 | Unauthenticated `/health/storage` exposes absolute host filesystem path | `path` dropped from public health payloads (kept in admin ops snapshot) |
| M8 | No Content-Security-Policy | CSP added (`default-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'`) — inline script/style allowances documented honestly |
| M9 | Config comment claims per-account WhatsApp app secrets take webhook precedence; verification actually uses app-level env only | comment corrected (config honesty); per-account secrets remain sending-only |
| M10 | netguard relies on `ipaddress` auto-unwrapping of IPv4-mapped IPv6 literals | explicit `ipv4_mapped` unwrap in `_ip_is_forbidden` |
| M11 | No account-level lockout (distributed password guessing unthrottled) | `failed_login_attempts` + `locked_until` on users (additive migration 0011), enforced on both login paths, knobs `QBIT_LOGIN_MAX_FAILED_ATTEMPTS` / `QBIT_LOGIN_LOCKOUT_MINUTES` |
| M12 | Worker container healthcheck only proves importability | worker writes a heartbeat file; compose healthcheck checks freshness |

### LOW (accepted residuals / small fixes applied)
| ID | Finding | Action |
|---|---|---|
| L1 | CSV formula injection on export (`=`, `+`, `-`, `@` lead chars) | fixed: cells neutralized with `'` prefix in CSV/XLSX writers |
| L2 | `X-Request-ID` echoed unvalidated | fixed: sanitized to 128 chars of `[A-Za-z0-9._-]` |
| L3 | 429 responses lack `Retry-After` | fixed: limiter-derived header on `RateLimitedError` |
| L4 | No MIME allowlist on uploads (mitigated: attachment disposition + XCTO + org authz) | accepted residual, documented |
| L5 | UI login user enumeration ("Account is deactivated") | fixed: constant-shape messages |
| L6 | Scraper error strings embed full fetched URLs (no secrets today; providers auth via headers) | accepted residual, documented |
| L7 | DNS rebinding TOCTOU in netguard (self-documented); pin-to-socket needs transport-level hook | accepted residual, documented |
| L8 | Rate limiting is process-local (single-worker deployment today; Redis swap documented as future work) | accepted residual, documented |
| L9 | Per-category upload limits, dead-letter listing UI | accepted residual |

## Readiness checklist (from §1)

security ✔(after fixes) · reliability ✔(after fixes) · performance (measured §47) · observability ✔(§34/36) ·
backup ✔(after fixes) · recovery ✔(documented+scripted) · deployment ✔(docs 29) · data safety ✔ ·
failure handling ✔(tests) · testing ✔(full suite) · release readiness → see final report
