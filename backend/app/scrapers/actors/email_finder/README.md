# Email Finder Actor (`email-finder` v1.0.0)

Public business email discovery (Phase 3 brief §27).

## What it does
- Validates the target (`website` URL or bare `domain`) against the SSRF policy.
- Scans the landing page + likely contact paths (`/contact`, `/about`,
  `/impressum`, `/support`, `/team`, ...) — **same domain only**.
- Extracts **publicly displayed** email addresses (mailto: links + visible text).
- Classifies each address: type ∈ {general, sales, support, info, contact,
  other} and confidence HIGH/MEDIUM/LOW via documented, deterministic
  local-part heuristics (no ML, no guessing beyond keywords).
- Yields one item per unique email with full provenance.

## Consent warning (§27 — always true, always surfaced)
`metadata.consent = "not_implied"`. A scraped email is NOT marketing consent.
Consent must be established and documented separately by the operator before
any outreach. This actor never attempts to obtain private addresses.

## Input
| field | type | default |
|---|---|---|
| website | URL | one of website/domain required |
| domain | hostname | " |
| crawl_depth | int 0–4 | 2 |
| max_pages | int 1–200 | 15 |
| request_timeout | int 1–120 | 20 |
| respect_robots | bool | true |

## Output
email, business_name, website, source, source_url, metadata(source_page,
email_type, confidence, pages_scanned, consent), scraped_at.

## Checkpoint / resume
Cursor = frontier + pages_fetched + already-found emails (dedup across resume).
