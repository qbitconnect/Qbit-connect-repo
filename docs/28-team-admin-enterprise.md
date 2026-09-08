# 28 — Team / Admin / Enterprise Engine (Phase 11)

Status: **implemented** · Migration `0007_team_admin_enterprise` · Tests `tests/test_enterprise.py` (28) + `tests/test_migrations.py` backfill-safety

This document describes the multi-user / team / admin / enterprise layer added in
Phase 11. It was designed as a strictly **additive** evolution of the existing
Phase 0–7 architecture: nothing was rebuilt, no role was removed, no column was
dropped, and every existing behavior keeps working after migration.

## 1. Tenancy model

The platform is organization-scoped. A single **Organization** is the tenant
boundary; every tenant-scoped table carries `organization_id`, and the
centralized authorization layer enforces it on every request.

| Model | Purpose |
|---|---|
| `Organization` | Tenant root: `slug` (unique), `status` (ACTIVE / SUSPENDED / ARCHIVED), `timezone`, `locale`, `settings_json` (visibility defaults) |
| `OrganizationMember` | User ↔ org link: `status` (ACTIVE…), `visibility_scope` (per-member override), `is_owner` |
| `Team` / `TeamMember` | Sub-groups for visibility and assignment; `TeamMember.is_lead` marks team leads |
| `Invitation` | Hashed one-time invite tokens with expiry, revocation and org/team pre-assignment |
| `UserSession` | Server-side session registry keyed by JWT `jti`; revocable, audited |
| `ApiKey` | Hashed, scope-limited machine credentials bound to one organization |
| `Notification` / `UserPreference` | In-app notification inbox and per-user profile preferences |
| `LeadAssignmentHistory` / `ConversationAssignmentHistory` | Immutable assignment audit (previous → new, actor, reason) |

### Default organization strategy (non-destructive migration)

Migration `0007` creates one deterministic **Default Organization**
(`00000000-0000-0000-0000-000000000001`, slug `default`) and links **every**
existing user to it (SUPER_ADMINs become owners). Every tenant-scoped table is
backfilled to that org: `leads`, `scrape_jobs`, `conversations`, `campaigns`,
`campaign_templates`, `sending_accounts`, `suppression_entries`, `connections`,
`files`, `audit_logs`, `import_batches`, `lead_exports`. Campaign ownership is
preserved (`owner_id = created_by` where owner was NULL) and existing
connections/sender accounts become `ORGANIZATION`-scoped, which reproduces the
pre-Phase-11 "any operator may send" behavior exactly. The migration only ADDS
tables, columns, indexes and permission rows — it never drops, truncates or
resets anything, and `downgrade` removes only what the revision created.

Legacy rows with a NULL `organization_id` (created by old code paths) always
remain visible — an un-migrated row can never silently disappear from an
operator's view.

## 2. Authorization architecture

Authorization is **layered, not replaced**:

```
require_permission(code)          capability check (existing, unchanged)
        │
AuthorizationService              org membership + org status + visibility
        │                         scope + resource access + safety guards
        ▼
visible query / 404
```

`AuthorizationService.can(user, permission, resource)` is the single decision
point (capability + org match + scope). Resource fetches go through
`get_visible_or_404(...)`, which answers **404** for foreign resources so IDs
are never confirmed to unauthorized callers.

### Visibility scopes (§9)

Four scopes, applied consistently across Leads, Conversations, Campaigns,
Scrape Jobs, Files, imports/exports and Reports:

| Scope | Sees |
|---|---|
| `ALL` | everything in the organization (shipped default — preserves pre-Phase-11 behavior) |
| `TEAM` | own + team-assigned + unassigned pool (queue-style workflow) |
| `ASSIGNED_ONLY` | own + assigned to the caller |
| `OWNED_ONLY` | strictly records the caller owns/created |

Resolution order (first match wins):
1. `organization_members.visibility_scope` (per-member override)
2. `organizations.settings_json["visibility_defaults"][ROLE]` (per-role default)
3. shipped role default `ALL`

Enforcement lives in the **backend** (`visibility_clause` / `apply_visibility`
/ `get_visible_or_404`); the UI merely hides what the API would refuse anyway.

### Cross-tenant protection (§28, CRITICAL)

- Every query is org-bounded; foreign-org resource IDs answer 404.
- `X-Organization-Id` may only target an organization the caller already
  belongs to — membership is **never** self-granted via the header.
- API keys resolve to a FIXED organization (the key row's org) and can never
  hop.
- Background tasks (scrape worker, data worker, campaign worker) carry the
  organization/user context from their DB rows; notifications emitted by
  workers are org-scoped.

## 3. RBAC changes

Existing roles and permissions are unchanged. 19 capability codes were ADDED
(`teams.*`, `invitations.*`, `inbox.view|reply|assign`, `leads.assign`,
`apikeys.*`, `sessions.view|revoke`, `security.view|manage`,
`notifications.view`), wired into the standard role matrix (ADMIN/SUPER_ADMIN
dominated; `notifications.view`/`inbox.view` granted to all roles). The
permission count grew 74 → 93; `seed_rbac` remains idempotent.

### Enterprise safety guards (§35)

- `assert_not_last_super_admin` blocks demotion/deactivation of the last
  SUPER_ADMIN (users API + admin UI both route through it).
- Self-role changes and self-deactivation are refused (self-approval
  protection).
- `SUPER_ADMIN` can never be granted via user creation or invitation.
- Bulk assignment is idempotent (same target = no-op) and history-logged.

## 4. Invitation system (§5)

- Token format: `secrets.token_urlsafe(32)`; **only a SHA-256 hash is stored**.
- Single-use (`accepted_at`), expiring (`QBIT_INVITATION_EXPIRY_HOURS`,
  default 168h; DB-overridable via `security.invitation_expiry_hours`),
  revocable, replay-protected (accept → second accept fails).
- Rate-limited per creator (`QBIT_RATE_LIMIT_INVITE_PER_HOUR`, default 30/h).
- The plaintext link is shown exactly once (POST response); it is never logged
  (audit metadata carries email + roles only).
- SUPER_ADMIN can never be granted through an invitation.

## 5. Sessions & security settings (§17–§18)

- Every login registers a `UserSession` row keyed by the token's `jti`;
  logout/revocation blocks the token immediately (401). Pre-Phase-11 tokens
  remain valid unless `users.tokens_revoked_before` excludes them.
- Deactivation/suspension revokes all sessions and outstanding tokens.
- `/admin/security` (UI) + `/api/v1/admin/security` expose invitation expiry
  and session TTL (DB-backed) plus visibility of password/login-limit envs.
- `/admin/security/sessions` UI lists and revokes sessions (own or others,
  permission-gated).

## 6. Connections & sender accounts (§15–§16)

Both `connections` and `sending_accounts` carry `organization_id`, `owner_id`,
`team_id`, `access_scope` (`ORGANIZATION` (default) | `TEAM` | `RESTRICTED`)
and — for connections — `last_checked_at`. `AuthorizationService.
can_use_connection` enforces the scope BEFORE any send: campaign launch and
conversation replies both check it. Credentials remain write-only (vault
references); no endpoint ever returns a secret.

## 7. API keys (§22–§23)

- Prefix lookup (`qbit_<8hex>`), SHA-256 hash of the secret at rest, plaintext
  shown exactly once at creation.
- Scope allowlist: `leads.read/write`, `campaigns.read/write`,
  `analytics.read`, `scraping.run`, `files.read`, `connections.read`. Scopes
  map onto platform capabilities and are intersected with the OWNER's
  permissions — a key can never grant more than its owner, and never admin
  powers.
- Creation is rate-limited; revocation is immediate; all key events are
  audited.

## 8. Files (§24)

Every file operation follows the chain: file_id → authentication → organization
check → permission → resource visibility check → action. Uploads/exports are
stamped with the caller's organization (API + UI + importer/exporter + scrape
export paths). Foreign-org files answer 404; raw filesystem paths are never
exposed.

## 9. Audit center (§19)

`audit_logs` gained `organization_id` + resource indexes. `/api/v1/admin/audit`
and `/admin/audit` provide server-side search (action substring, resource
type/ID, actor, outcome). The log is append-only: no update/delete route
exists, and administrators cannot edit historical records.

## 10. Admin console UI (§31–§34)

Server-rendered pages in the existing dark QBIT design system
(`app/ui/admin.py`, `app/ui/admin_security.py`, `app/templates/admin/*`):

| Route | Contents |
|---|---|
| `/admin` | Overview — real counts only (workflows honestly report N/A until that engine ships) |
| `/admin/users` + `/admin/users/{id}` | Lifecycle (INVITED/ACTIVE/SUSPENDED/DEACTIVATED), roles editor, guard rails |
| `/admin/teams` + `/admin/teams/{id}` | Create/rename/activate, membership, team leads |
| `/admin/roles` | Read-only enforced permission matrix (prevents matrix drift) |
| `/admin/invitations` | Create (link shown once), revoke, status |
| `/admin/connections` | Access-scope editor for connections + sender accounts |
| `/admin/api-keys` | Create (one-time display), revoke |
| `/admin/security` | Security settings + session management |
| `/admin/audit` | Searchable immutable audit log |
| `/admin/settings` | Org profile + per-role visibility defaults |

Sidebar shows the ADMIN section only for users holding the relevant
`*.view` capabilities. In-app notifications are surfaced at `/notifications`
with mark-as-read; the API offers `/api/v1/notifications` + unread counts.

## 11. Notification events (§25)

In-app events are emitted (never breaking the caller) for: invitation
created/accepted, lead/conversation assignment, campaign completion/failure,
scrape-job completion/failure, account deactivation, session revoked, API key
revoked. The emitter is the extension point for future e-mail/push channels.

## 12. API surface added

| Router | Endpoints |
|---|---|
| `/api/v1/teams` | list, create, get, patch, members add/remove |
| `/api/v1/invitations` | accept (public), list, create, revoke |
| `/api/v1/api-keys` | list, create, revoke |
| `/api/v1/sessions` | `/me`, `/revoke-all`, list, revoke |
| `/api/v1/notifications` | list, unread-count, mark read |
| `/api/v1/conversations` | list, get, messages, reply, assignment, bulk-assignment |
| `/api/v1/admin` | overview, audit search, settings get/patch, security get/patch |
| `/api/v1/users` | `+/me/preferences` (get/patch), status/search, activity |
| `/api/v1/leads` | `+assignment`, `+assignment-history`, `assigned_to_me` filter |

## 13. Operational notes

- New env knobs: `QBIT_INVITATION_EXPIRY_HOURS`,
  `QBIT_RATE_LIMIT_INVITE_PER_HOUR`, `QBIT_RATE_LIMIT_APIKEY_PER_HOUR`
  (see `.env.example`).
- New indexes cover `organization_id` on every tenant table, assignment
  columns, `users.status`, and audit resource lookups; list endpoints bulk-load
  roles/counts to avoid N+1 queries.
- The worker (`app.worker`) continues to run unaffected; jobs now notify their
  owners and inherit org context through the job rows.
