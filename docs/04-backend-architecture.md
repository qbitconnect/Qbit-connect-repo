# 04 — Backend Architecture & API Boundaries

> Covers required architecture docs: **Backend Architecture** + **API Boundary Design**
> (Final Output L)

## 1. Layered Architecture

```
HTTP Layer      FastAPI routers: request parsing, auth, validation, response shaping
Service Layer   Business logic, transactions, event emission, job submission
Repository      SQLAlchemy queries; one repository per aggregate; no business logic
Adapters        External I/O (scrapers, providers, storage) behind interfaces
Workers         Celery tasks calling the same service layer (single source of truth)
```

Rules:

- Routers never touch SQLAlchemy sessions directly.
- Services own transactions and emit domain events; they are callable from both HTTP
  and workers, so behavior never forks between the two paths.
- Repositories are the only place raw SQL/ORM queries live.
- All cross-cutting concerns (auth, request-id, audit, rate limiting) are ASGI
  middleware or FastAPI dependencies — never inline code.

## 2. Backend Folder Architecture (Final Output D)

```
qbit-backend/
├── app/
│   ├── main.py
│   ├── core/
│   │   ├── config.py            # pydantic-settings, env-driven
│   │   ├── security.py          # password hash, session signing, csrf
│   │   ├── logging.py           # structlog JSON, request-id binding
│   │   ├── errors.py            # error envelope, exception handlers
│   │   └── db.py                # async engine, session factory
│   ├── auth/                    # routers, services, models
│   ├── rbac/
│   ├── dashboard/
│   ├── scrapers/
│   │   ├── registry.py          # discovery, manifest validation
│   │   ├── base.py              # BaseScraper interface
│   │   ├── normalization.py     # → normalized Lead DTO
│   │   └── plugins/…            # one package per scraper
│   ├── jobs/                    # manager, states, checkpoints, history
│   ├── leads/
│   ├── marketing/
│   │   ├── engine.py            # channel-agnostic pipeline
│   │   ├── eligibility.py       # Communication Eligibility Service
│   │   ├── templates.py         # central template engine
│   │   └── channels/{whatsapp,email,sms}/
│   ├── connections/
│   ├── inbox/
│   ├── campaigns/
│   ├── exports/
│   ├── analytics/
│   ├── storage/
│   └── events/                  # envelope, outbox writer, catalog
├── workers/
│   ├── celery_app.py            # queues: scrape, marketing, email, export, analytics
│   ├── tasks/                   # thin task wrappers → services
│   └── beat_schedule.py         # periodic: health, rollups, backups trigger
├── migrations/
├── templates/  static/          # admin UI (doc 05)
└── tests/
```

## 3. Configuration (Environment Variables)

All configuration is env-driven (`pydantic-settings`), grouped by concern. Secrets are
read from the environment or a `.env` file that is never committed.

| Group | Examples |
|---|---|
| Core | `QBIT_ENV`, `QBIT_BASE_URL`, `QBIT_SECRET_KEY`, `QBIT_DATA_DIR=/qbit-data` |
| Database | `QBIT_DB_URL=postgresql+asyncpg://…`, pool sizes |
| Redis/Queue | `QBIT_REDIS_URL`, queue concurrency, worker counts |
| Auth | `QBIT_SESSION_TTL`, `QBIT_PASSWORD_MIN`, `QBIT_MAX_LOGIN_ATTEMPTS` |
| Vault | `QBIT_VAULT_KEY` (32-byte Fernet key, rotate per doc 17) |
| Providers | `QBIT_WA_*`, `QBIT_SMTP_*`, `QBIT_SES_*` (or stored via vault) |
| Scraping policy | `QBIT_SCRAPER_CONCURRENCY`, `QBIT_SCRAPER_RPS`, `QBIT_SCRAPER_TIMEOUT_S` |
| Storage | `QBIT_STORAGE_BACKEND=local`, `QBIT_MAX_UPLOAD_MB` |
| Observability | `QBIT_LOG_LEVEL`, `QBIT_LOG_FORMAT=json` |

## 4. API Boundary Design (Final Output L)

Versioned JSON API under `/api/v1` + HTMX fragment endpoints for the SSR UI under
`/ui/...`. All endpoints (except login/health) require an authenticated session and a
matching permission. List endpoints share one convention: pagination (`?page`, 
`?page_size≤100`), filters, `?sort=`, and return `{data, meta:{page, total}}`.

| Prefix | Resource | Representative endpoints | Permission |
|---|---|---|---|
| `/api/v1/auth` | Session | `POST /login`, `POST /logout`, `GET /me` | public/admin |
| `/api/v1/dashboard` | Aggregates | `GET /summary`, `GET /health` | viewer |
| `/api/v1/scrapers` | Registry | `GET /`, `GET /:id`, `POST /:id/validate` | operator |
| `/api/v1/jobs` | Jobs (all kinds) | `POST /`, `GET /`, `GET /:id`, `POST /:id/pause` `/resume` `/cancel`, `GET /:id/logs` | operator |
| `/api/v1/leads` | Leads | `GET /`, `GET /:id`, `PATCH /:id`, `POST /merge`, `POST /import` | operator |
| `/api/v1/leads/tags` | Tags | CRUD | manager |
| `/api/v1/campaigns` | Campaigns | `POST /`, `GET /`, `GET /:id`, `POST /:id/start` `/pause` `/resume` `/stop`, `POST /:id/test-send`, `GET /:id/messages` | manager |
| `/api/v1/templates` | Templates | CRUD + `POST /:id/preview` | manager |
| `/api/v1/connections` | Accounts | `GET /`, `POST /`, `POST /:id/test`, `DELETE /:id` | admin |
| `/api/v1/inbox` | Conversations | `GET /`, `GET /:id/messages`, `POST /:id/reply`, `POST /:id/assign`, `POST /:id/close` | operator |
| `/api/v1/exports` | Exports | `POST /`, `GET /`, `GET /:id`, `GET /:id/download` | operator |
| `/api/v1/analytics` | Rollups | `GET /scraping`, `GET /whatsapp`, `GET /email`, `GET /campaigns` | viewer |
| `/api/v1/settings` | System | users, roles, storage, system config | super_admin |
| `/api/v1/webhooks/whatsapp` | Inbound | provider callbacks, signature-verified | signature |
| `/ui/*` | HTMX fragments | module tables, forms, progress partials | per-module |

**Error envelope (uniform):**

```json
{ "error": { "code": "VALIDATION_ERROR", "message": "…", "request_id": "…",
             "details": { "field": "…" } } }
```

**Security invariants on the boundary:** credentials and tokens are never present in
any response; file downloads go through authorized streaming endpoints (no static dir
listing); webhooks verify HMAC signatures before processing; admin-only routes are
double-guarded (session + role).

## 5. Background Task Submission Pattern

```python
# Service layer — the only place tasks are enqueued
job = jobs_manager.create(kind="scrape", payload=validated_input, created_by=user.id)
enqueue("q.scrape", job.id)          # routes to scraper queue
return JobAccepted(job_id=job.id)    # HTTP responds immediately
```

Task wrappers in `workers/tasks/` are thin: reconstruct context, call the service,
translate exceptions into job failure events. Idempotency keys (`job.id`, 
`message.id`) make retries safe (doc 09, 16).
