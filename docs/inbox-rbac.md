# Inbox RBAC & Visibility (Phase 8 §49–§52)

## Permissions (backend-enforced — the UI is never the boundary)

| Permission | Grants |
|---|---|
| `inbox.view` | view conversations, messages, activity, counters, search |
| `inbox.reply` | send replies (AND the channel permission below) |
| `inbox.whatsapp.reply` | send WhatsApp replies |
| `inbox.email.reply` | send email replies |
| `inbox.assign` | assign / unassign conversations |
| `inbox.manage` | unrestricted visibility (ALL) + configuration scope |
| `inbox.add_notes` | add internal conversation notes |
| `inbox.change_status` | change conversation status |
| `inbox.change_priority` | change / reset conversation priority |
| `inbox.link_lead` | link / unlink a lead to a conversation |
| `inbox.create_lead` | create a lead from an inbox contact |

Role matrix (seeded by `seed_rbac` / migration `0007`):

| Role | Inbox access |
|---|---|
| SUPER_ADMIN | everything |
| ADMIN | everything |
| MANAGER | view, reply (both channels), assign, notes, status, priority, link/create lead |
| OPERATOR | view, reply (both channels), assign, notes, status, priority, link/create lead |
| VIEWER | `inbox.view` only |

A reply requires `inbox.reply` **and** the channel-specific permission
(`inbox.whatsapp.reply` / `inbox.email.reply`) — enforced in
`api/v1/inbox.py::_require_channel_reply_permission`, not in the browser.

## Visibility scoping (§50)

Resolved in SQL by `InboxWorkspace._visibility_clause`:

- `inbox.manage` holders (admins) → `ALL`
- otherwise the `QBIT_INBOX_VISIBILITY` setting decides:
  - `ALL` (default) — the whole team's queue
  - `ASSIGNED_ONLY` — conversations assigned to the user **plus unassigned**
    (a team queue must stay workable; another agent's assigned thread is
    invisible)
  - `TEAM` behaves as `ALL` until a team model exists (no team table in the
    platform yet — nothing is hard-coded)

Out-of-scope conversations return **404, never 403** — existence is not
leaked. This applies to detail, messages, mutations, and bulk operations
(each row is visibility-checked; unauthorized rows are skipped silently).

## Audit logging (§51)

Every mutation writes an append-only `AuditLog` row through the existing
redacting `AuditService`: `inbox.message_sent`, `inbox.message_retry`,
`inbox.status_changed`, `inbox.priority_changed`, `inbox.assigned`,
`inbox.note_added`, `inbox.lead_linked`, `inbox.lead_unlinked`,
`inbox.lead_created`, `inbox.bulk_read/unread/…`. Workflow detail also lands
in the per-conversation `conversation_events` activity timeline.

Never logged: message credentials/secrets, provider tokens, webhook
signatures, SMTP passwords. Audit metadata passes the platform-wide
`redact()` filter.

## Data privacy (§52)

- API responses are scoped: conversation detail exposes display-safe account
  fields only (`has_credentials`, never `config_metadata`, never tokens)
- internal notes are visible to the team only and are NEVER sent to the
  customer (§27)
- no destructive bulk operations exist; history is preserved
