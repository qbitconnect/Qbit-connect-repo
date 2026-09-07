# Unified Inbox (Phase 8)

The unified inbox is the team's single workspace for WhatsApp and Email
conversations. It is **provider-independent**: channels plug in through a
normalizer, and future channels (SMS, Instagram, Facebook) require no inbox
changes.

## Architecture

```
WHATSAPP / EMAIL provider event
        ↓  (webhook, signature-verified)
   Message Normalizer          services/inbox/normalizer.py
        ↓  UnifiedInboundMessage
   Conversation Engine         services/inbox/engine.py
        ↓                      thread match → lead match → message → unread
   Conversation / Message      (PostgreSQL)
        ↓
   Inbox API + UI              api/v1/inbox.py · ui/inbox.py · /inbox
```

- `services/inbox/normalizer.py` — provider payloads → one channel-neutral
  shape. Only provider-received data is carried; nothing is inferred.
- `services/inbox/engine.py` — ingestion, threading, lead matching, workflow
  (status/priority/assignment), delivery-status mirroring, activity events.
- `services/inbox/workspace.py` — server-side list/search/filters/pagination
  and unread counters, with SQL-enforced visibility scoping.
- `services/inbox/reply.py` — reply validation, idempotency and outbox
  enqueue. `services/inbox/outbox.py` — worker-side delivery through the
  SAME provider abstraction campaigns use.

## UI

`/inbox` — three panes:

| Left | Center | Right |
|---|---|---|
| search, channel/status/assignment/priority filters, conversation rows (channel icon, name, preview, time, unread badge, priority, status) | thread header (status/priority/assignee controls), chronological bubbles grouped by day, delivery states, composer with template fallback | lead context (business, contact, phone, email, geo, source, status, quality, tags), link/create-lead actions, internal notes, activity timeline |

Realtime (§37, §60): polling fallback — sidebar unread badge every 12s, open
thread every 8s with append-check (reconnects never duplicate messages).
Empty states are real ("No conversations yet." / "Select a conversation to
view messages." / "Contact not linked to a lead."); provider/account problems
surface normalized reasons ("The WhatsApp customer-service window has
closed…", "Sending account is … — replies are unavailable").

## Inbox endpoints (JWT + `inbox.*` RBAC)

```
GET    /api/v1/inbox/conversations                 list + filters + search + pagination
GET    /api/v1/inbox/conversations/{id}            detail (lead context, account, assignee)
GET    /api/v1/inbox/conversations/{id}/messages   cursor-paginated timeline (50/page)
POST   /api/v1/inbox/conversations/{id}/messages   queue reply (202; idempotent)
POST   /api/v1/inbox/conversations/{id}/messages/{mid}/retry   retry failed reply
POST   /api/v1/inbox/conversations/{id}/read       mark read
POST   /api/v1/inbox/conversations/{id}/unread     mark unread
PATCH  /api/v1/inbox/conversations/{id}/status     OPEN/PENDING/WAITING/RESOLVED/CLOSED
PATCH  /api/v1/inbox/conversations/{id}/priority   NORMAL/HIGH/URGENT (null = reset)
POST   /api/v1/inbox/conversations/{id}/assign     assign/unassign user
POST   /api/v1/inbox/conversations/{id}/notes      internal note
GET    /api/v1/inbox/conversations/{id}/activity   events + notes timeline
POST   /api/v1/inbox/conversations/{id}/link-lead      link existing lead
POST   /api/v1/inbox/conversations/{id}/unlink-lead    unlink lead
POST   /api/v1/inbox/conversations/{id}/create-lead    create lead from contact
GET    /api/v1/inbox/unread-count                  total / whatsapp / email / assigned_to_me
GET    /api/v1/inbox/search                        server-side message search
POST   /api/v1/inbox/bulk                          read/unread/assign/status/priority
```

UI data endpoints live under `/ui/inbox/*` (cookie session; same service
layer; same permissions). Bulk **delete** intentionally does not exist —
conversation history is preserved (§46).

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `QBIT_INBOX_REOPEN_ON_REPLY` | `true` | inbound reply reopens RESOLVED/CLOSED threads (§32) |
| `QBIT_INBOX_VISIBILITY` | `ALL` | `ALL` or `ASSIGNED_ONLY` (§50; TEAM = ALL until teams exist) |
| `QBIT_INBOX_WHATSAPP_WINDOW_HOURS` | `24` | provider customer-service window for free-text replies (§22) |
| `QBIT_INBOX_OUTBOX_BATCH_SIZE` | `25` | replies claimed per worker cycle |
| `QBIT_INBOX_OUTBOX_MAX_ATTEMPTS` | `5` | TRANSIENT retry ceiling before honest failure |
| `QBIT_INBOX_MESSAGE_PAGE_SIZE` | `50` | default timeline page |

## What is NOT here (non-goals, §70)

No workflow automation, no auto-replies, no drip campaigns, no AI message
generation, no WhatsApp Web/QR automation, no restriction bypass. Media is a
metadata foundation only (§42): type/filename/mime/size may be stored; no
server-side arbitrary URL downloads.
