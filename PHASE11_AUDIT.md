# PHASE 11 AUDIT — Pre-Implementation Repository Review

Date: 2026-09-08 · Baseline commit: `9570ffb` · Baseline tests: **536 passed** (0 failed)

Purpose: record what Phase 11 (Team / Admin / Enterprise engine) may REUSE, must
EXTEND, and must ADD — without duplicating or breaking anything.

## 1. Verified existing state (Phases 0–8)

| Area | Status | Location |
|---|---|---|
| FastAPI app factory, DI via `app.state` | ✅ | `app/main.py` |
| Auth: argon2id + HS256 JWT bearer (stateless, `jti` present) | ✅ | `app/core/security.py`, `api/v1/auth.py` |
| UI cookie session (wraps same JWT, HttpOnly) | ✅ | `app/ui/__init__.py` |
| RBAC tables: roles, permissions, user_roles, role_permissions | ✅ | `app/models/rbac.py` |
| Roles: SUPER_ADMIN, ADMIN, MANAGER, OPERATOR, VIEWER (spec baseline matches) | ✅ | `app/services/rbac.py` |
| Capability-based permissions (~70 codes) + role matrix seed | ✅ | `app/services/rbac.py` |
| Centralized enforcement: `require_permission()` dependency | ✅ | `app/api/deps.py` |
| Append-only audit logs (`audit_logs`) + AuditService | ✅ | `app/models/audit.py`, `services/audit.py` |
| Users CRUD API (list/get/create/patch/password) | ✅ | `api/v1/users.py` |
| Roles API | ✅ | `api/v1/roles.py` |
| System settings (DB-backed, typed) | ✅ | `models/setting.py`, `services/settings.py` |
| Files: metadata + path-safe storage + downloads | ✅ | `models/file.py`, `services/files.py`, `api/v1/files.py` |
| Connections (WhatsApp/email), encrypted secret vault refs | ✅ | `models/connection.py`, `models/messaging.py` |
| Leads workspace (tags/notes/views/imports/exports/duplicates) | ✅ | `models/lead.py`, `models/scrape.py` |
| Marketing: campaigns/templates/sending accounts/suppression/queue | ✅ | `models/marketing.py` |
| Conversations + Messages (webhook-driven; NO inbox API/UI yet — nav item disabled) | ✅ models only | `models/messaging.py` |
| Background worker (scrape queue, leases, checkpoints) | ✅ | `app/worker.py` |
| Rate limiter (sliding window) — login + unsubscribe | ✅ | `core/ratelimit.py` |
| Security headers, request IDs, uniform error envelope | ✅ | `app/main.py`, `core/errors.py` |
| Alembic migrations 0001–0006 (SQLite-compatible, tested) | ✅ | `alembic/versions/` |
| Workflow automation / Analytics engines | ❌ not in repo | — (Phase 9/10 not present; Phase 11 adds no workflow features) |

## 2. Ownership / tenancy fields today

| Resource | created_by | owner | org | team | assigned |
|---|---|---|---|---|---|
| leads | ✅ | — | — | — | — |
| conversations / messages | — | — | — | — | — |
| campaigns | ✅ | — | — | — | — |
| campaign_templates / sending_accounts / suppression | ✅/— | — | — | — | — |
| connections | ✅ | — | — | — | — |
| files | ✅ | — | — | — | — |
| scrape_jobs | ✅ | — | — | — | — |
| saved_views | owner_id ✅ (PRIVATE/TEAM/GLOBAL) | — | — | — | — |
| audit_logs | actor_user_id | — | — | — | — |
| users | — (global) | — | — | — | — |

Conclusion: **no tenant boundary exists** → Phase 11 introduces Organization +
Teams with a safe default-organization backfill (single-org strategy).

## 3. Gaps Phase 11 must add (additive only)

1. Models: Organization, OrganizationMember, Team, TeamMember, Invitation,
   UserSession (server-side session registry keyed by JWT `jti`), ApiKey,
   Notification, UserPreference, LeadAssignmentHistory, ConversationAssignmentHistory.
2. Columns: `users.status`, `users.tokens_revoked_before`; `organization_id` on all
   tenant-scoped tables; `assigned_user_id`/`assigned_team_id` on leads+conversations;
   `owner_id/team_id/updated_by` on campaigns; `access_scope/owner_id/team_id/
   last_checked_at` on connections; `organization_id` on audit_logs.
3. Migration 0007 (additive; backfill default org + members + org ids).
4. Permission additions: teams.*, invitations.*, apikeys.*, notifications.*,
   leads.assign, inbox.view/reply/assign, audit.view already exists, security.manage.
5. Centralized AuthorizationService layered ON TOP of existing `require_permission`
   (reuse — not replacement): org membership check, visibility scopes
   (ALL/TEAM/ASSIGNED_ONLY/OWNED_ONLY), resource accessors, last-admin guard.
6. Session management (login creates session rows; revoke → invalidates JWT by jti;
   `tokens_revoked_before` covers pre-phase tokens for backward compatibility).
7. Invitation flow (hashed token, expiry, one-time use, revocation, rate limit).
8. API keys (prefix lookup, hash-only storage, allowlisted scopes, last_used_at).
9. In-app notifications + preferences.
10. Admin UI (`/admin/...`) reusing the existing dark QBIT visual language.
11. Assignment APIs for leads & conversations (+ history, bulk, idempotent).
12. Connection access control (ORGANIZATION/TEAM/RESTRICTED) enforced before send.

## 4. Constraints honored

- No destructive migration; existing rows keep working (default org backfill).
- Existing role matrix preserved; new permissions added, never removed.
- Existing tests must stay green (536 baseline).
- Secrets never returned by APIs (connections already use vault refs — keep).
- Workflow/analytics features are out of scope (not present in codebase).

---

## 5. Implementation addendum (2026-09-08, post-implementation)

All gaps in §3 were closed additively:

1. Models → `app/models/enterprise.py` (Organization, OrganizationMember, Team,
   TeamMember, Invitation, UserSession, ApiKey, Notification, UserPreference,
   Lead/ConversationAssignmentHistory) + tenancy columns on all tenant tables
   (+ import_batches / lead_exports / sending_accounts scope columns).
2. Migration `0007_team_admin_enterprise` — additive; default-org backfill;
   verified non-destructive by `test_0007_backfill_is_safe_and_preserves_ownership`.
3. AuthorizationService (`app/services/authorization.py`) layered on
   `require_permission`; visibility scopes; `get_visible_or_404` IDOR guards;
   last-admin guard; connection access checks.
4. Permissions 74 → 93 (idempotent migration + seed).
5. Invitations / sessions / API keys / notifications / preferences APIs +
   conversations (list/get/messages/reply/assignment/bulk) + admin API.
6. Tenancy enforcement added to files, scrape-jobs, imports/exports, UI export.
7. Worker org-context + owner notifications (scrape + campaign terminal states).
8. Admin console UI (`app/ui/admin.py`, `app/ui/admin_security.py`,
   `app/templates/admin/*`) + `/invite` public accept page + `/notifications`.
9. Tests: `tests/test_enterprise.py` (28) — cross-tenant isolation, visibility
   scopes, invitation security (expiry/replay/revocation/rate limit), session
   lifecycle, API-key scopes+IDOR, enterprise guards, assignment history,
   notifications, audit immutability. Suite total: **565 passed**.
