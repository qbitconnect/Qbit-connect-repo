# Message Processing (Phase 8)

The end-to-end life of a message, both directions, all channels.

## Inbound pipeline (§38–§40)

```
Provider webhook (signature-verified)
  → normalize (channel → UnifiedInboundMessage)
  → store ProviderEvent  (UNIQUE provider+provider_event_id → duplicate = no-op)
  → thread match (conversation key, §5)
  → lead match (exact normalized, §6)
  → Message row (idempotent per conversation+provider_message_id)
  → conversation timers + unread_count += 1
  → reopen rule if thread was RESOLVED/CLOSED (§32)
  → campaign REPLIED linkage (when the contact was a recipient)
  → inbox event (UI polling picks it up)
```

WhatsApp path: `POST /api/v1/webhooks/whatsapp` (existing Phase 6 pipeline,
now feeding the unified engine). Email path:
`POST /api/v1/webhooks/email/inbound/{provider}` — see `inbox-webhooks.md`.

Safety properties (§40):
- **duplicate webhook** → one message (ProviderEvent gate + per-conversation
  message idempotency)
- **out-of-order events** → timestamps and statuses never regress
- **provider retries** → no double counting anywhere
- **database hiccups** → per-webhook transaction boundaries; a failed
  application leaves the raw ProviderEvent stored for forensics

## Out-of-order delivery events (§41)

Delivery statuses move **forward only**:
`SENDING → SENT → DELIVERED → READ` (FAILED only from the in-flight states).
If READ arrives before DELIVERED, the state stays READ; the DELIVERED
timestamp is still first-write recorded. Campaign recipients run the same
forward-only rule via the Phase 5 state machine — both stay consistent.

## Outbound replies (§24–§26)

```
UI / API reply (202)
  → ReplyService validation:
      account exists + not UNHEALTHY + ACTIVE
      recipient address present
      suppression re-check (RECIPIENT_SUPPRESSED:<reason> on hit)
      channel rules:
        WHATSAPP  free text ONLY inside the 24h customer-service window
                  (last inbound within window) — otherwise 409
                  TEMPLATE_REQUIRED with approved-template selection
                  variables render from the linked lead only
        EMAIL     subject (default "Re: <subject>"), In-Reply-To/References
                  resolved from the last Message-ID in the thread
  → Message(status=SENDING) + inbox_outbox row
      idempotency_key = inbox:<conversation_id>:<client_message_id> (UNIQUE)
  → worker OutboxService loop (isolated, like the campaign loop)
      claim (lease + attempts) → credentials (vault → env, per call)
      → provider.send / provider.send_session_text (WhatsApp text)
  → SENT (provider_message_id, sent_at) or classified failure
      TRANSIENT → exponential backoff (honors retry-after) up to max attempts
      PERMANENT/CONFIGURATION → immediate honest failure
```

**Idempotency (§25):** the same `client_message_id` returns the SAME message
(`created: false`) — double-clicks and retries cannot duplicate sends.
**Retry (§26):** only confirmed-FAILED messages can be retried; the retry
re-queues the SAME message row — never a second provider request while the
first attempt is unconfirmed.

Campaign-sent messages join the same threads (§61): after a confirmed
provider send, the campaign worker records an OUTBOUND message with the
campaign id in metadata — the inbox shows complete communication history,
and customer replies link back to campaign recipients (REPLIED).

## Media foundation (§42)

Message metadata may carry `media_type`, `provider_media_id`, `filename`,
`mime_type`, `size`. No media is downloaded in Phase 8; if/when download is
added it MUST go through StorageService with type/size validation and the
Phase 3 SSRF guards — arbitrary remote URL downloads stay forbidden.

## Content security (§44)

Inbound email HTML is stored as received (message immutability) but is
**sanitized with nh3 (allowlist) at display time** and rendered inside a
sandboxed iframe with a plain-text fallback — scripts, inline handlers,
`javascript:` URLs and unsafe embeds can never execute. WhatsApp bodies are
rendered as plain text (escaped).

## Performance (§47, §48, §56)

- conversation list: offset pagination ≤200/page; latest-message preview via
  an indexed `(conversation_id, created_at)` key — never a per-conversation
  history load
- message timeline: cursor pagination (newest 50 → scroll upward)
- indexes: conversations(status, assigned_user_id, priority, unread_count,
  channel, last_message_at, account+phone, account+email);
  messages(conversation_id+created_at, conversation_id+external_message_id,
  provider_message_id); inbox_outbox(status+available_at, idempotency UNIQUE)
- search is server-side LIKE over indexed columns (lead name/phone/email,
  subject, body, provider_message_id) — the browser never loads the mailbox
- isolated load tests (100k conversations / 1M messages) run against a test
  database only; no synthetic data is ever written to production
