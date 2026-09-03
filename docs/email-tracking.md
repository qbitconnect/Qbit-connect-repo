# Email Tracking (Phase 7)

Open and click tracking are **optional, per-campaign settings** (§29–§31). Nothing is tracked unless the campaign enables it.

## Campaign settings (§31)

Stored in `campaigns.campaign_metadata` (PATCH `/api/v1/campaigns/{id}` with `campaign_metadata`):

| Key | Default | Meaning |
|---|---|---|
| `track_opens` | `QBIT_EMAIL_DEFAULT_TRACK_OPENS` (false) | append the 1×1 tracking pixel |
| `track_clicks` | `QBIT_EMAIL_DEFAULT_TRACK_CLICKS` (false) | rewrite links to signed redirects |
| `append_unsubscribe_footer` | `QBIT_EMAIL_APPEND_UNSUBSCRIBE_FOOTER` (true) | append the real unsubscribe footer when the template lacks one |
| `company_name`, `company_address` | — | rendered via `{{company_name}}` / `{{company_address}}` |

**Accuracy disclaimer:** open/click tracking is inherently approximate — email clients block or prefetch pixels and strip click wrapping. The UI/analytics label these numbers directional; the platform never claims exact measurement.

## Open tracking (§29)

```
GET /api/v1/email/track/open/{tracking_key}?s=<hmac>
```

- the pixel URL carries the recipient's unguessable `tracking_key` — **never a raw database id**
- response: a 1×1 transparent GIF (200), even for forged keys — no state leaks
- a valid signature records `EmailTrackingEvent(OPEN)` + the recipient's first `opened_at` (immutable) + a `MESSAGE_OPENED` campaign event

## Click tracking (§30)

At compose time (only when enabled) http/https links are rewritten:

```
https://dest.example.com/offer
 → /api/v1/email/track/click/{tracking_key}?u=<base64url(dest)>&s=<hmac>
```

At click time:

1. verify the HMAC signature over (`click`, key, encoded destination)
2. re-validate the destination scheme — **only http/https redirect**; `javascript:`, `data:`, `file:` and friends are refused at wrap AND redirect time (no open redirects)
3. record `EmailTrackingEvent(CLICK, url)` + first `clicked_at` + `MESSAGE_CLICKED`
4. `302` to the destination

Forged URLs (bad signature) answer `400` and never redirect.

## Data model

- `email_tracking_events`: append-only evidence (campaign, recipient, event_type OPEN/CLICK, url, user_agent, occurred_at) with indexes per §50
- `campaign_recipients.tracking_key`: UNIQUE, unguessable per-recipient key issued lazily at send time
- `campaign_recipients.opened_at / clicked_at`: first-event timestamps used by analytics

## Privacy posture

- tracking is off unless enabled; the default follows the platform privacy defaults (`QBIT_EMAIL_DEFAULT_TRACK_*` = false)
- recipient-level evidence stays inside the operator's own platform (self-hosted); no third-party tracker is involved
- the user agent string is stored truncated (300 chars) for forensics only

## Reply tracking foundation (§32, §33)

A full inbound-mailbox UI is a later phase; Phase 7 ships the **normalized backend interface**:

- inbound email ingestion (`EmailInboundService.record_inbound_email`) normalizes sender/recipient/subject/body/message-id/thread headers into `Conversation` + `Message` rows matched by (sending account, contact email)
- threading uses `Message-ID` / `In-Reply-To` / `References` — never subject matching alone — and links replies to the originating campaign recipient (`REPLIED` status + event)
- no fake inbox exists: nothing is displayed unless real inbound data arrived

Endpoints ingest inbound mail the same way webhooks do (signed), so a reply-mailbox provider or an inbound Email-API can be wired in without schema changes.
