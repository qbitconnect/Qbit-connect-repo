# QBIT — Self-Hosted Enterprise Data & Marketing Operations Platform

> **Status: PHASE 11 COMPLETE — Team / Admin / Enterprise engine on the
> Phase 0–10 foundation. Multi-user organizations + teams + invitations (hashed one-time
> tokens) + centralized AuthorizationService with 4-level visibility scopes (backend-
> enforced) + lead/conversation assignment with immutable history + connection access
> scopes + API keys (hashed, scoped, one-time display) + revocable server-side sessions +
> immutable audit center + admin console UI — on top of the full stack: scraper actor
> engine, lead workspace, marketing engine, WhatsApp + Email providers, unified inbox
> + conversations, workflow automation, analytics & reporting.**

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
| Phase | Phase 0 (architecture) + Phase 2 (core foundation) + Phase 3 (scraper/actor engine) + Phase 4 (lead workspace) + Phase 5 (marketing engine foundation) + Phase 6 (WhatsApp Business provider integration) + Phase 7 (email marketing provider integration) + Phase 8 (unified inbox + conversations) + Phase 9 (automation & workflow engine) + Phase 10 (analytics & reporting engine) + Phase 11 (team / admin / enterprise engine) complete — see `docs/25`–`29` completion/architecture reports + `docs/whatsapp-*.md` + `docs/email-*.md` + `docs/unified-inbox.md` + `docs/conversations.md` + `docs/message-processing.md` + `docs/inbox-rbac.md` + `docs/inbox-webhooks.md` + `docs/automation*.md` + `docs/workflow-*.md` + `docs/analytics.md` + `docs/metric-definitions.md` + `docs/report-builder.md` + `docs/28-team-admin-enterprise.md` + `PHASE11_AUDIT.md` |
| Backend | FastAPI + SQLAlchemy 2 async + Alembic + Argon2 + RBAC + centralized AuthorizationService + scraper actor engine + lead data workspace + marketing engine + workflow automation + analytics & reporting — `backend/` |
| Version | `0.11.0` (backend/app/__init__.py) |
| Tests | 777 passing (`backend/tests`, isolated per-test database) |
| Next step | Phase 12 — production deployment hardening |

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
| 30 | [Marketing Engine](docs/marketing-engine.md) | Phase 5 architecture, database, campaign lifecycle, queue/retry/idempotency, providers, security |
| 31 | [Campaigns](docs/campaigns.md) | campaign model, audiences, validation report, API, UI wizard |
| 32 | [Templates](docs/templates.md) | safe variable engine, channel rules, preview, deletion semantics |
| 33 | [Eligibility](docs/eligibility.md) | checks ladder, suppression list, opt-outs, consent rule |
| 34 | [Providers](docs/providers.md) | provider contract, registry, mock provider, event interface, sending accounts |
| 35 | [WhatsApp Provider](docs/whatsapp-provider.md) | Phase 6 adapter, Cloud API client, error normalization, health probe, compliance boundary |
| 36 | [WhatsApp Connections](docs/whatsapp-connections.md) | multi-account architecture, connection flow, secret management, permissions, API |
| 37 | [WhatsApp Templates](docs/whatsapp-templates.md) | provider template sync, approval gating, variable mapping, campaign usage |
| 38 | [WhatsApp Webhooks](docs/whatsapp-webhooks.md) | verification + signature security, idempotency, state machine, conversations foundation |
| 39 | [Email Provider](docs/email-provider.md) | Phase 7 SMTP + generic Email API adapters, error normalization, uncertain-delivery rule, compliance boundary |
| 40 | [Email Connections](docs/email-connections.md) | multi-sender accounts, validation/health flow, secret handling, reputation foundation, API |
| 41 | [Automation Engine](docs/automation.md) | Phase 9 architecture, workflow lifecycle, versioning, idempotency, loop prevention, scheduler, delays |
| 42 | [Workflow Triggers](docs/workflow-triggers.md) | 21 triggers: lead/campaign/conversation/message/scheduled, event bus, filters |
| 43 | [Workflow Conditions](docs/workflow-conditions.md) | declarative operators, AND/OR/NOT groups, field catalog, validation |
| 44 | [Workflow Actions](docs/workflow-actions.md) | lead/assignment/conversation/communication actions, safety ladder, campaign action |
| 45 | [Workflow Execution](docs/workflow-execution.md) | state machine, claiming/locking, delays, retry, monitor, analytics, recovery |
| 46 | [Workflow Builder](docs/workflow-builder.md) | UI pages, lightweight builder, validation display, persistence |
| 47 | [Automation Security](docs/automation-security.md) | declarative-only engine, threat controls, RBAC matrix, secrets/privacy |
| 41 | [Email Templates](docs/email-templates.md) | subject + sanitized HTML + plain text, variable allowlist, real unsubscribe, header-injection guards |
| 42 | [Email Delivery](docs/email-delivery.md) | pipeline, eligibility, launch gates, idempotency, retry, bounce/complaint handling, analytics |
| 43 | [Email Webhooks](docs/email-webhooks.md) | signed webhook contract, replay protection, event normalization, idempotency |
| 44 | [Email Unsubscribe](docs/email-unsubscribe.md) | token security (hash-at-rest), public opt-out flow, suppression enforcement |
| 45 | [Email Tracking](docs/email-tracking.md) | opt-in open/click tracking, signed redirects, privacy posture, reply-tracking foundation |
| 46 | [Unified Inbox](docs/unified-inbox.md) | Phase 8 provider-independent inbox architecture, endpoints, configuration, UI layout, non-goals |
| 47 | [Conversations](docs/conversations.md) | conversation/message schema, threading keys, lead matching + MATCH_REVIEW, unread logic, status workflow |
| 48 | [Message Processing](docs/message-processing.md) | inbound/outbound pipelines, idempotency, out-of-order safety, reply window rules, performance/indexes |
| 49 | [Inbox RBAC](docs/inbox-rbac.md) | inbox permission catalog, role matrix, backend visibility scoping, audit + privacy |
| 50 | [Inbox Webhooks](docs/inbox-webhooks.md) | WhatsApp + email-inbound webhook contracts, idempotency guarantees, troubleshooting |
| 51 | [Analytics Engine](docs/analytics.md) | Phase 10 architecture, domains, caching, aggregates, timezone rules, RBAC, diagnostics |
| 52 | [Metric Definitions](docs/metric-definitions.md) | every metric's source table, calculation, denominator, missing-data behavior |
| 53 | [Report Builder](docs/report-builder.md) | saved reports, allowlisted configs, background runs, snapshots, exports, ownership |

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
