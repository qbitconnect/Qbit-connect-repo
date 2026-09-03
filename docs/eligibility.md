# Eligibility, Suppression & Opt-Out

## Eligibility engine

Before a recipient may enter the send queue (and again immediately before
each send), the eligibility engine evaluates:

| # | Check | Failure reason |
|---|---|---|
| 1 | contact address present for the channel | `MISSING_EMAIL` / `MISSING_PHONE` |
| 2 | address structurally valid | `INVALID_ADDRESS` |
| 3 | channel known and servable | `CHANNEL_UNAVAILABLE` |
| 4 | lead exists, not archived, not merged away | `LEAD_UNAVAILABLE` |
| 5 | suppression status (global list) | `SUPPRESSED` |
| 6 | opt-out evidence | `UNSUBSCRIBED` |
| 7 | opt-in / permission metadata | `NO_OPT_IN` |

Result: `ELIGIBLE` or `INELIGIBLE` + reason. Ineligible recipients REMAIN in
the snapshot marked `INELIGIBLE` with their reason, so validation reports can
show exactly who would be skipped and why.

### Consent rule (important)

**A scraped email or phone is NOT consent.** Leads must carry explicit opt-in
metadata (`metadata_json.marketing_opt_in == true`) to be eligible. The
wizard defaults to enforcing opt-in; the check runs at eligibility, and the
worker re-verifies suppression before every send.

## Suppression list

Global do-not-contact with four entry types:

- `EMAIL` — normalized email address
- `PHONE` — normalized phone (same normalizer as the lead dedup keys)
- `LEAD` — entire lead (any channel)
- `CHANNEL` — channel-wide entry

Reasons: `UNSUBSCRIBED`, `BOUNCED`, `COMPLAINT`, `BLOCKED`, `MANUAL`,
`PROVIDER_RESTRICTION`. Entries may be channel-scoped or global
(`channel = NULL` applies to all channels). Adds are idempotent; the same
(type, address, channel) can never duplicate.

All checks are batched `IN`-queries — eligibility for a 1,000-lead chunk
costs a constant number of queries, not one per lead.

## Opt-out records

Unsubscribes are **evidence**: append-once, never deleted, never silently
re-enabled. Recording an opt-out automatically creates the matching
suppression entry, making the address immediately unsendable. Suppression
entries backed by an opt-out record refuse removal via both service and API.

## API

```
GET    /api/v1/suppression-list                    list entries (filters)
POST   /api/v1/suppression-list                    add entry
DELETE /api/v1/suppression-list/{id}               remove (refused for opt-outs)
GET    /api/v1/suppression-list/opt-outs           list opt-outs
POST   /api/v1/suppression-list/opt-outs           record opt-out (auto-suppresses)
```

Permissions: `suppression.view` to read; `suppression.manage` to mutate.
