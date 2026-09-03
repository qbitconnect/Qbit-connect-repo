# QBIT — Self-Hosted Enterprise Data & Marketing Operations Platform

> **Status: PHASE 7 COMPLETE — Email Marketing Provider Integration on the Phase 0–4
> foundation (+ marketing engine core & WhatsApp provider abstraction). 390 tests passing.
> Email sending accounts (SMTP/Email API, multi-account) + templates + eligibility/suppression
> + unsubscribe tokens + campaign pipeline + signed webhooks + bounce/complaint handling +
> optional open/click tracking + analytics + connections/campaigns/templates UI.**

QBIT is a self-hosted **admin/operations portal** — not a marketing website — for
managing: Data Scraping · Lead/Data Management · Multi-channel Marketing ·
Communication Accounts · Campaigns · Results/Exports · Messaging/Inbox · Analytics ·
System Administration.

Primary surface: **Login → Authentication → Admin Dashboard → QBIT Control Center**
(dark, professional, Apify-inspired developer-tool aesthetic with QBIT's own branding).

---

## Repository Status

| Item | State |
|---|---|
| Repo | `qbitconnect/Qbit-connect-repo` (private) |
| Audit result | Empty repository — greenfield build (see `docs/01-repository-audit.md`) |
| Phase | Phase 0 (architecture) + Phase 2 (core foundation) + Phase 3 (scraper/actor engine) + Phase 4 (lead workspace) + Phase 7 (email marketing provider; incl. the marketing-engine core & provider abstraction Phase 7 requires) complete — see `docs/25`–`28` completion reports |
| Backend | FastAPI + SQLAlchemy 2 async + Alembic + Argon2 + RBAC + scraper actor engine + lead data workspace + marketing/email provider stack — `backend/` |
| Version | `0.7.0` (backend/app/__init__.py) |
| Tests | 390 passing (`backend/tests`, isolated per-test database) |
| Next step | Phase 8 — remaining platform modules (inbox UI on the new conversation/message foundation, dashboard, settings UI) |

## Documentation Index

| # | Document | Contents |
|---|---|---|
| 01 | [Repository Audit](docs/01-repository-audit.md) | Audit method, verdict, binding constraints, environment assumptions |
| 02 | [System Architecture](docs/02-system-architecture.md) | Principles, tech stack, **Diagram A** overall system, **Diagram B** request flow, ADRs |
| 03 | [Component Architecture](docs/03-component-architecture.md) | Module map, dependency rules, module trees, scraper plugin + marketing adapter architecture |
| 04 | [Backend Architecture](docs/04-backend-architecture.md) | Layers, folder architecture, env config, **API boundary design** |
| 05 | [Frontend Architecture](docs/05-frontend-architecture.md) | SSR+HTMX approach, **UI sitemap**, QBIT design system |
| 06 | [Database Architecture](docs/06-database-architecture.md) | **ERD**, every-table justification, dedup, indexing, **Diagram F** lead lifecycle |
| 07 | [Storage Architecture](docs/07-storage-architecture.md) | `/qbit-data` layout, StorageService, **Diagram J**, capacity/health |
| 08 | [Scraper Architecture](docs/08-scraper-architecture.md) | Plugin contract, catalog, compliance gate, **Diagram C** scraping flow |
| 09 | [Job/Queue Architecture](docs/09-job-queue-architecture.md) | Celery/Redis topology, states, retries/checkpoints, **Diagram G** |
| 10 | [Marketing Architecture](docs/10-marketing-architecture.md) | Channel-agnostic engine, cross-channel invariants |
| 11 | [WhatsApp Architecture](docs/11-whatsapp-architecture.md) | Official-API provider abstraction, 1→100 accounts, **Diagram D** campaign flow |
| 12 | [Email Architecture](docs/12-email-architecture.md) | SMTP/SES/Graph adapters, bounce handling, **Diagram E** campaign flow |
| 13 | [Connection Architecture](docs/13-connection-architecture.md) | Connection Center, encrypted credentials, **Diagram H** lifecycle |
| 14 | [Inbox Architecture](docs/14-inbox-architecture.md) | Unified inbox pipeline, normalizer, conversations |
| 15 | [Campaign Architecture](docs/15-campaign-architecture.md) | Engine pipeline, states, **Eligibility Service**, template system |
| 16 | [Event Architecture](docs/16-event-architecture.md) | Envelope, full catalog, outbox, **Diagram N**, traceability |
| 17 | [Security Architecture](docs/17-security-architecture.md) | Controls, secrets model, **Diagram L** auth flow, webhooks |
| 18 | [RBAC Architecture](docs/18-rbac-architecture.md) | 5 roles, permission matrix, **Diagram M** |
| 19 | [Export Architecture](docs/19-export-architecture.md) | CSV/XLSX/JSON, export record, optional Drive connector |
| 20 | [Deployment Architecture](docs/20-deployment-architecture.md) | Compose topology, env vars, upgrades, **Diagram K**, sizing |
| 21 | [Backup & Recovery](docs/21-backup-recovery-architecture.md) | What/schedule/retention/verification, restore runbook, RPO/RTO |
| 22 | [Observability](docs/22-observability-architecture.md) | Structured logs, correlation chain, health, metrics, incident trace |
| 23 | [Scaling · Cost · Failure](docs/23-scaling-cost-failure.md) | 10k/100k/1M+ scaling, cost strategy, **all 17 failure scenarios** |
| 24 | [Implementation Phases](docs/24-implementation-phases.md) | Phase 0→14 with exit criteria and dependency graph |
| 25 | Phase 2 completion report | core foundation checklist |
| 26 | Phase 3 completion report | scraper engine checklist |
| 27 | [Leads](docs/leads.md) | lead schema, provenance, search/filter/sort, tags/notes/activity, RBAC, API |
| 28 | [Import / Export](docs/import-export.md) | formats, column mapping, duplicate strategies, batches, rejected reports, streaming exports |
| 29 | [Data Workspace](docs/data-workspace.md) | architecture, dedup ladder, performance, observability |
| 30 | [Email Provider](docs/email-provider.md) | BaseMarketingProvider contract, adapters, error normalization, non-goals |
| 31 | [Email Connections](docs/email-connections.md) | sending accounts, SMTP/API configuration, validation, secret safety |
| 32 | [Email Templates](docs/email-templates.md) | variables whitelist, sanitization, unsubscribe variable, preview |
| 33 | [Email Delivery](docs/email-delivery.md) | queue pipeline, idempotency, retry, bounce/complaint handling |
| 34 | [Email Webhooks](docs/email-webhooks.md) | signature/replay verification, payload contract, idempotent application |
| 35 | [Email Unsubscribe](docs/email-unsubscribe.md) | token architecture, public flow, terminal suppression |
| 36 | [Email Tracking](docs/email-tracking.md) | opt-in open/click tracking, safe redirects, privacy honesty |
| — | [Phase 7 Audit](PHASE7_AUDIT.md) | Phase 7 baseline audit + gap analysis + implementation plan |

## Final Architecture Output (Brief §43, items A–T)

| Item | Where |
|---|---|
| A. Recommended technology stack | doc 02 §2 |
| B. Complete system diagram | doc 02 — Diagram A |
| C. Complete module tree | doc 03 §1–3 |
| D. Backend folder architecture | doc 04 §2 |
| E. Frontend folder architecture | doc 03 §4, doc 05 |
| F. Scraper plugin architecture | doc 03 §5, doc 08 §2 |
| G. Marketing adapter architecture | doc 03 §6, doc 10 |
| H. Database ERD | doc 06 §2 |
| I. Storage architecture | doc 07 (+ Diagram J) |
| J. Queue/worker architecture | doc 09 (+ Diagram G) |
| K. Authentication/RBAC architecture | docs 17, 18 (+ Diagrams L, M) |
| L. API boundary design | doc 04 §4 |
| M. Event model | doc 16 (+ Diagram N) |
| N. Deployment architecture | doc 20 (+ Diagram K) |
| O. Backup architecture | doc 21 |
| P. Security model | docs 17–18 |
| Q. Failure recovery model | doc 23 §3 (17 scenarios) |
| R. Scaling model | doc 23 §1 |
| S. UI sitemap | doc 05 §2 |
| T. Implementation phases | doc 24 |

## Required Diagrams (Brief §37)

A Overall system → doc 02 · B Request flow → doc 02 · C Scraping flow → doc 08 ·
D WhatsApp campaign flow → doc 11 · E Email campaign flow → doc 12 ·
F Lead lifecycle → doc 06 · G Job lifecycle → doc 09 · H Connection lifecycle →
doc 13 · I Message lifecycle → doc 16 (message event flow) · J Storage → doc 07 ·
K Deployment → doc 20 · L Authentication flow → doc 17 · M RBAC → doc 18 ·
N Event architecture → doc 16. *(All Mermaid, render natively on GitHub.)*

## Compliance Red Lines (enforced in design & future code review)

Public/authorized data only · no CAPTCHA/login/paywall/security bypass · no anti-bot
or ban evasion · no stealth automation · no unauthorized bulk messaging · official
business APIs only · eligibility + suppression before every send · secrets server-side
only · self-hosted, no license lock, no mandatory cloud.

---

**⏸ Per the governing brief: implementation STOPs here until the architecture is
approved. Next action for the product owner: review docs 01–24, then approve Phase 1.**
