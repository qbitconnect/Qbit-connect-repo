# 01 — Repository Audit (Phase 0)

> Status: **COMPLETE** · Phase: 0 · Requires approval before any code is written.

## 1. Audit Method

The audit followed the command brief: inspect the entire repository before proposing
anything, and design the architecture around what actually exists — never around
assumptions. The inspection was performed against the remote repository
`qbitconnect/Qbit-connect-repo` (GitHub, private), which was cloned in full and examined
for code, configuration, history, and artefacts.

Checks performed:

| Check | Command / Method | Result |
|---|---|---|
| Clone integrity | `git clone` full history | OK — cloned successfully |
| Commit history | `git log --all` | **0 commits — history is empty** |
| Branches | `git branch -a` | No branches with content |
| Backend code | tree walk | None present |
| Frontend code | tree walk | None present |
| Database files / migrations | tree walk | None present |
| Scraper modules | tree walk | None present |
| API definitions | tree walk | None present |
| Deployment config (Docker/systemd/CI) | tree walk | None present |
| Environment variable files | tree walk | None present |
| Storage mechanisms | tree walk | None present |
| Secrets scan | tree walk | None present (clean) |

## 2. Audit Verdict

**The repository is a freshly initialized, empty repository.** There is:

- No existing frontend to preserve.
- No existing backend to preserve.
- No current database technology, authentication system, or user data.
- No existing scraper modules, APIs, storage layer, or workers.
- No deployment configuration, environment files, or secrets.
- No technical debt, duplicate functionality, or legacy security problems.

### Consequences for the architecture

1. **"Do not break existing functionality"** becomes a *forward-looking baseline*:
   once implemented, the module boundaries defined in this architecture become the
   protected surfaces. Nothing exists today that can be damaged, and no production or
   demo data can be lost — there is nothing to migrate, reset, or drop.
2. The architecture is a **greenfield design**, but it is deliberately *not* exotic.
   It is built from boring, proven, self-hostable components (PostgreSQL, Redis,
   FastAPI, Celery) so that the first implementation is low-risk and the operational
   cost stays near zero.
3. The protected-data rules in the brief (no destructive migrations, no deleting user
   data) are encoded into the design itself: Alembic-only schema evolution, append-only
   audit/event tables, soft-delete conventions, and a backup system that never
   auto-deletes (see docs 06, 16, 21, 23).

## 3. Non-Negotiable Constraints Extracted from the Brief

These constraints are binding for every subsequent phase and are restated here as the
governance baseline:

**Product constraints**

- QBIT is a self-hosted **admin/operations portal**, not a marketing website.
- Primary surface: Login → Authentication → Admin Dashboard → QBIT Control Center.
- Visual language: dark, professional, dense-but-readable, card-based, developer-tool
  aesthetic inspired by platforms like Apify — but with QBIT's own branding only.
- Modules must stay independent: Authentication, Dashboard, Scraping, Leads, Marketing,
  Connections, Inbox, Campaigns, Exports, Analytics, Settings.

**Compliance constraints (hard red lines)**

- Public/authorized data collection only.
- **FORBIDDEN**: CAPTCHA bypass, login bypass, paywall bypass, security bypass,
  anti-bot evasion, rate-limit evasion, stealth/detection-evasion mechanisms, credential
  theft, private-profile extraction, unauthorized access, ban evasion, randomized
  stealth automation, unauthorized bulk messaging.
- **REQUIRED**: respectful request rates, configurable concurrency, retries with
  backoff, timeouts, policy-aware behavior, source-specific restrictions, opt-out and
  suppression handling, audit trails.
- WhatsApp/email sending must use official/supported business APIs and provider
  integrations with eligibility checks before every send.

**Technical constraints**

- Self-hosted first: VPS / dedicated server / Docker / Linux.
- No mandatory cloud storage, cloud database, SaaS dependency, license key, activation
  server, or subscription validation. Application authentication ≠ software licensing.
- Local/mounted storage is the primary persistent store; cloud (e.g. Google Drive) may
  only ever be an *optional* export connector.
- Secrets never leave the server; never exposed to any frontend client.
- Long-running work (scraping, campaigns, exports) must run in background workers —
  never inside synchronous HTTP requests.
- Every external integration must sit behind an adapter; every major operation must be
  auditable; every critical workflow must have tests.

**Process constraints**

- No uncontrolled "implement everything at once" — work proceeds in the phased plan
  (Phase 0 → Phase 14, doc 24).
- This document set is the Phase 0 deliverable. **Implementation waits for explicit
  approval.**

## 4. Environment Assumptions

| Item | Assumption |
|---|---|
| Production host | Linux VPS or dedicated server (Ubuntu LTS / Debian) |
| Runtime | Docker + Docker Compose (systemd fallback documented in doc 20) |
| Reverse proxy | Nginx (TLS termination, security headers, rate limiting) |
| Python | 3.12+ |
| PostgreSQL | 16+ |
| Redis | 7+ |
| Data root | Configurable via `QBIT_DATA_DIR` (default `/qbit-data`) |
| Timezone | Server-local, UTC in database |

## 5. What Must NOT Be Broken (from Phase 1 onward)

Once implementation begins, these become the protected invariants:

1. Existing lead data, exports, and files on disk are never auto-deleted.
2. Schema changes only via forward-only Alembic migrations reviewed before apply.
3. Adapter interfaces (scraper, channel, storage, provider) are versioned; upgrades
   must not silently change stored data semantics.
4. The event/audit trail is append-only.
5. Any change to authentication, RBAC, or secret storage requires explicit review.
