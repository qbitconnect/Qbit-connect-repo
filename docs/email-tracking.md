# Email Tracking — Open & Click (Phase 7 §29–§31)

## Opt-in model

Tracking is **per-campaign opt-in** (`track_opens`, `track_clicks`); the
platform defaults are OFF (`QBIT_MARKETING_DEFAULT_TRACK_*`). Nothing is
tracked unless the operator explicitly enables it.

## Open tracking (§29)

When enabled, a 1×1 pixel is appended to the HTML body:

```
<img src="/t/open/{token}" width="1" height="1" alt="">
```

- `token = base64(json{campaign,recipient}).HMAC-SHA256(QBIT_SECRET_KEY)[:32]`
  — internal database IDs are never exposed raw and cannot be forged
- `GET /t/open/{token}` records an `OPEN` tracking event, sets the first
  `opened_at`, advances the recipient to READ and bumps the campaign counter
  — exactly once per recipient (subsequent opens are logged, not counted)
- responses are `Cache-Control: no-store`

## Click tracking (§30)

When enabled, every http/https link in the HTML body is rewritten:

```
https://vendor.com/offer  →  /t/click/{signed-token}
GET /t/click/{token}  →  record CLICK  →  302 → https://vendor.com/offer
```

- tokens are HMAC-signed with the destination embedded — clients cannot forge
  arbitrary redirect targets (no open redirect)
- **only `http://` and `https://` destinations may be redirected.**
  `javascript:`, `data:`, `vbscript:`, `file:`, protocol-relative and
  whitespace-trick URLs are refused — dangerous links are never rewritten and
  were already removed by the HTML sanitizer
- first click per recipient sets `clicked_at` + bumps the counter

## Privacy honesty (§31)

- open/click rates are **indicative, not exact**: many clients block images or
  prefetch pixels; text-only renders record nothing. The UI/API states this
  limitation explicitly (§34 notes).
- individual recipient delivery is preserved — tracking never leaks one
  recipient to another
- disable both flags for privacy-strict campaigns; delivery, bounce and
  complaint analytics are unaffected

## Endpoints

```
GET /t/open/{token}    → 1×1 PNG (no-store)
GET /t/click/{token}   → 302 redirect (http/https only)
```
