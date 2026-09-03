# Email Unsubscribe (Phase 7)

One-click, login-free opt-out for EMAIL campaigns (§12, §13, §14).

## The link

Every marketing email contains a functional unsubscribe mechanism:

- templates may render `{{unsubscribe_url}}` explicitly, and/or
- the platform appends an honest footer (HTML + plain text) with the same link

The URL is always the platform's **real** endpoint:

```
GET /unsubscribe/{token}
```

Fake unsubscribe links are never generated. EMAIL campaigns cannot launch while `QBIT_EMAIL_UNSUBSCRIBE_BASE_URL` is unconfigured — the platform refuses to send mail it cannot make compliant.

## Token security (§51)

- `secrets.token_urlsafe(32)` — 256 bits of entropy, unguessable
- the database stores **only the SHA-256 hash** (`email_unsubscribe_tokens.token_hash`, UNIQUE); a leaked database cannot be used to unsubscribe victims or forge links
- nothing predictable is encoded in the token (no lead id, no email, no campaign id)
- tokens expire (`expires_at`, 365 days) and are single-address; re-subscription is a separate, explicit, compliant action

## Flow (§13)

```
Token (email link)
 ↓ sha256 → hash lookup
Resolve recipient
 ↓
Confirm opt-out (public HTML page — no login, rate-limited per IP)
 ↓
OptOutRecord + SuppressionEntry (channel=EMAIL, reason=UNSUBSCRIBED)
 ↓
future campaigns → eligibility → INELIGIBLE/UNSUBSCRIBED → SKIPPED
```

## Rules (§14)

- opt-out evidence (`opt_out_records`) is append-once and **never deleted**
- the matching suppression entry cannot be removed while opt-out evidence exists — no silent reactivation
- the confirmation page tells the truth on repeat visits (idempotent) and states that addresses are never re-enabled automatically
- abuse protection: per-IP sliding-window rate limit; unknown/expired tokens answer `404` without leaking whether an address exists

## API surface

- public: `GET /unsubscribe/{token}` (HTML confirmation)
- evidence listing: `GET /api/v1/suppression-list/opt-outs?channel=EMAIL` and `GET /api/v1/suppression-list?channel=EMAIL` (permissions `suppression.email.view` / `suppression.email.manage`)
- operators may add EMAIL suppressions manually; webhook-driven suppression (hard bounce/complaint) uses the same tables

## Pre-send enforcement

Suppression is checked **twice**: at eligibility (batched, before queueing) and again in the worker immediately before send (lists can change mid-run). A suppressed recipient is skipped with `SUPPRESSED` / `UNSUBSCRIBED` and never reaches the provider.
