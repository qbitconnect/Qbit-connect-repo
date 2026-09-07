# Conversations (Phase 8)

One conversation per contact per channel per sending account — the stable
thread key.

## Conversation model (`conversations`)

| Field | Notes |
|---|---|
| `channel` | `WHATSAPP` / `EMAIL` (extensible) |
| `sending_account_id` | the sending identity the thread belongs to |
| `lead_id` | attached ONLY when a real Lead matches (nullable) |
| `external_contact_id` | provider-supplied display id (WhatsApp wa_id / profile name, email address) |
| `contact_phone` / `contact_email` | normalized matching keys |
| `subject` | EMAIL threads |
| `status` | `PENDING` (unresolved contact) → `OPEN` → `WAITING` / `RESOLVED` / `CLOSED` |
| `priority` | `NORMAL` / `HIGH` / `URGENT` |
| `assigned_user_id` | assignee (§28); `assigned_team_id` reserved for a future team model |
| `last_message_at` / `last_inbound_at` / `last_outbound_at` | thread timers; never regress on out-of-order events (§41) |
| `unread_count` | inbound messages not yet read by the team |
| `match_status` | `MATCHED` / `UNMATCHED` / `MATCH_REVIEW_REQUIRED` |
| `closed_at` | set while `CLOSED`, cleared on reopen |

## Message model (`messages`)

`direction` INBOUND/OUTBOUND · `provider_message_id` · `external_message_id`
(client idempotency id for replies) · `message_type` TEXT/TEMPLATE/EMAIL/… ·
`body` · `subject` · `sender`/`recipient` · `status` · `metadata` (bounded,
secret-free) · `created_at` + delivery timestamps `sent_at` `delivered_at`
`read_at` `failed_at`.

## Message immutability (§4)

Historical messages are never rewritten. Body/sender/recipient stored at
arrival stay exactly as received; only legitimate normalized status updates
(status + timestamps) are applied, and every workflow action appends a
`conversation_events` row instead of mutating history.

## Threading (§5)

- **WhatsApp**: `(sending_account, normalized phone)` — the provider's chat
  identity. Never subject-based.
- **Email**: `(sending_account, normalized contact email)`; Message-ID /
  In-Reply-To / References are stored on each message for threading display
  and campaign-reply association. Fallback is never subject-only.
  Duplicate conversations per email cannot occur — the key is the contact,
  not the message.

## Lead matching (§6)

Incoming contact identity → normalized → `leads.phone_norm` /
`leads.email_norm` exact comparison:

- **0 match** → conversation stays lead-less (`UNMATCHED`, status PENDING).
- **1 match** → auto-attach (`MATCHED`, status OPEN).
- **>1 match** → `MATCH_REVIEW_REQUIRED`; nothing is attached (scenario 4).
  The UI shows "match review" on the row; an operator links the correct lead
  manually (Link lead / Create lead actions, §8).

Lead creation from the inbox uses ONLY provider-received data: the normalized
phone/email and, when the provider supplied one, the display name. Company,
address and industry are never fabricated (§7). Linking preserves all
conversation history and backfills `lead_id` on existing messages; unlinking
never deletes anything (§8).

## Unread logic (§14)

- inbound message → `unread_count += 1`
- `POST …/read` → 0 (opening in the UI does this explicitly — appearing in
  the list NEVER marks a conversation read)
- `POST …/unread` → max(1, current)
- bulk variants via `POST /api/v1/inbox/bulk`

## Status workflow (§31, §32)

OPEN (active) · PENDING (waiting for internal action) · WAITING (waiting for
the customer) · RESOLVED (handled) · CLOSED (completed). Transitions are
validated against the vocabulary; `closed_at` tracks the closed state.
A customer reply to a RESOLVED/CLOSED conversation reopens it
(`QBIT_INBOX_REOPEN_ON_REPLY`, default on) and records
`CONVERSATION_REOPENED` in the activity timeline — no duplicate thread is
silently created.

## Assignment & history (§28, §29)

Assign to a user (or unassign) via `POST …/assign`. Every change appends
`ASSIGNED` / `UNASSIGNED` with previous → new value and the actor — the full
history is queryable through `GET …/activity`.
