# Email Unsubscribe (Phase 7 §12–§14, §51)

## Token architecture (§13, §51)

- token = `secrets.token_urlsafe(32)` — cryptographically random, unguessable
- only the **SHA-256 hash** is stored (`unsubscribe_tokens.token_hash`)
- NO lead_id / email / campaign_id is encoded anywhere in the token
- single-use (`used_at`), expiry-tracked (default 365 days), IP recorded on use
- the raw token is shown exactly once — inside the recipient's email link

## Flow

```
Email contains {{unsubscribe_url}} = {QBIT_PUBLIC_BASE_URL}/unsubscribe/{token}
   ↓  GET (no login — legitimate opt-out must be frictionless, §13)
Confirmation page ("Confirm opt-out" button)
   ↓  POST
Token resolved (hash lookup) → used_at stamped
Suppression created: channel=EMAIL, reason=UNSUBSCRIBED, source=unsubscribe
Consent evidence updated: opt_in_status=OPTED_OUT
   ↓
Confirmation page ("You are unsubscribed")
```

## Effects (§14)

- the address is suppressed for ALL future EMAIL campaigns
- eligibility consults the suppression registry BEFORE queueing — unsubscribed
  recipients are `SKIPPED` with reason `UNSUBSCRIBED`, never sent
- the suppression is **terminal**: manual removal via the API/UI is refused.
  Clearing it requires the explicit, audited re-subscription flow with consent
  evidence (`SuppressionService.resubscribe`). Silent reactivation is impossible.
- unsubscribe events arriving via provider webhooks (list-unsubscribe
  processing) are processed identically through the webhook pipeline

## Abuse resistance (§48)

The endpoint is public but protected by: unguessable 256-bit tokens, hash-only
storage, single-use semantics, expiry, and size-capped inputs. Guessing yields
the neutral "link not valid" page — no information leak.

## API surface

```
GET  /unsubscribe/{token}   → HTML confirmation page
POST /unsubscribe/{token}   → performs the opt-out
```
