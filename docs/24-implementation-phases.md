# 24 — Implementation Phases

> Covers required doc: **Implementation Phases** (Brief §44) · Final Output T

**Governing rule (Brief §44): implementation proceeds in controlled phases — never one
uncontrolled operation. Each phase has entry/exit criteria and ends with a review.
No phase starts without the previous phase accepted.**

## Phase 0 — Repository Audit + Architecture ✅ (this document set)

| Item | Content |
|---|---|
| Deliverables | Repo audit (doc 01) + full architecture docs 02–24 + diagrams A–N + ERD + phases |
| Exit criteria | Architecture approved by product owner; open questions resolved |

## Phase 1 — Authentication + Admin Shell

Argon2id auth, sessions, login/lockout, audit of auth events; admin layout (sidebar,
topbar), design system components (doc 05), RBAC middleware + seeded roles, first
SUPER_ADMIN bootstrap. **Exit:** login → dashboard shell works, permission matrix
enforced and tested.

## Phase 2 — Core Database + Storage

PostgreSQL + Alembic baseline for identity/jobs/leads/system tables; StorageService +
local backend; `files` registry; health endpoints. **Exit:** migrations forward-only,
storage read/write/verify tested, `/readyz` green.

## Phase 3 — Scraper Engine

Plugin registry + BaseScraper contract, policy client (rate/concurrency/timeout/
backoff), job manager + Celery queues + checkpoints, normalization + dedup pipeline.
**Exit:** one reference scraper runs end-to-end as a background job with resume.

## Phase 4 — Scraper UI + Jobs

Scraper catalog cards, schema-driven input forms, validation, job list/detail with
live progress (SSE), logs, cancel/pause/resume. **Exit:** full Flow A (doc 08)
operable by an OPERATOR.

## Phase 5 — Lead Database

Leads table UI (dense table, filters, detail timeline), tagging, import wizard (CSV/
XLSX), merge/dedup workflows. **Exit:** 10k-row import + dedup verified; timeline
events populate.

## Phase 6 — Export System

Export manager (CSV/XLSX/JSON), background generation, `/exports` browser, authorized
downloads, export metadata. **Exit:** Flow A completes to local-storage download.

## Phase 7 — Marketing Engine

Channel-agnostic engine: eligibility service, central templates (versioning/preview/
test-send), campaigns CRUD + audience snapshots + dispatcher + throttles. **Exit:**
dry-run campaign produces correct eligibility accounting without a live provider
(stub adapter in tests).

## Phase 8 — WhatsApp Integration

Official WhatsApp Business Platform adapter, multi-account connections + webhooks
(verified, idempotent), template approval sync, delivery/read/reply events, account
health. **Exit:** Flow B works with an official test account; compliance red lines
re-verified in code review.

## Phase 9 — Email Integration

SMTP adapter (default) + SES/Graph adapters, bounce/complaint handling, unsubscribe/
suppression, reply detection → inbox. **Exit:** Flow C works against a real mailbox;
bounces suppress correctly.

## Phase 10 — Inbox

Unified inbox (filters, threads, assignment, close), reply via channel adapters,
lead timeline linkage. **Exit:** WhatsApp + email replies appear in one inbox and on
the lead.

## Phase 11 — Analytics

Event rollup worker, dashboard KPIs (Brief §4 widget list), `/analytics` views per
domain (Brief §21 metrics). **Exit:** dashboard matches event-sourced truth within one
rollup interval.

## Phase 12 — Security Hardening

CSP/HSTS review, rate-limit tuning, session hardening, vault key rotation drill,
dependency audit, permission-matrix conformance tests, audit coverage review.
**Exit:** external threat-model walkthrough passes.

## Phase 13 — Testing

Unit + integration per module, e2e for Flows A/B/C, failure-scenario tests for all 17
cases (doc 23), load tests at 100k-lead profile, backup/restore drill. **Exit:** CI
green; runbooks rehearsed.

## Phase 14 — Production Deployment

Compose hardening (non-root, internal networks), TLS, monitoring hookup, seed first
admin, runbook handover, go-live checklist. **Exit:** production smoke tests pass;
backup verified within 24 h.

## Dependency Graph

```
P0 ─ P1 ─ P2 ─ P3 ─ P4 ─ P5 ─ P6
              P3 ─ P7 ─ P8 ─ P10 ─ P11
              P7 ─ P9 ─┘        │
                   P6 ──────────┘
              P8/P9/P10 ─ P12 ─ P13 ─ P14
```

## Definition of Done (all phases)

Tests for every critical workflow · audit on every major operation · adapters for
every external integration · background workers for every long task · docs updated
with any deviation from this architecture · no compliance red line crossed.
