# Website Actor (`website` v1.0.0)

General public website scraper (Phase 3 brief §26).

## What it does
- Crawls pages of **one target domain** (BFS from the input URL, plus common
  contact-page hints tried early).
- Extracts **publicly present** data only: page title, meta description, emails
  (mailto: links + visible text), phone numbers (tel: links), social profile
  links, semantic `<address>` hints, optional page text sample.
- Yields one raw item per page that contains contact information; the platform
  dedup merges pages of the same site into one lead (website-host match).

## Hard limits & safety
- Same-domain crawling only; external links are recorded as metadata, never
  crawled (brief §42).
- robots.txt respected through the shared policy HTTP client (§17, §26).
- SSRF protection on every URL and redirect hop; private/loopback/link-local
  targets are refused (§41).
- `max_pages`, `max_depth`, per-request timeout, response-size cap, retry with
  backoff — all enforced by the platform (§32, §43).
- No authentication, no private content, no security bypass, no evasion of any
  platform control (§17, §55).

## Input
| field | type | default | notes |
|---|---|---|---|
| url | URL | required | http/https |
| max_pages | int | 20 | 1–500 |
| max_depth | int | 2 | 0–5 |
| extract_emails | bool | true | |
| extract_phones | bool | true | |
| extract_social_links | bool | true | |
| extract_text | bool | false | adds a text sample to metadata |
| respect_robots | bool | true | |
| request_timeout | int | 20 | seconds, 1–120 |

## Output (normalized lead fields)
business_name, email, phone, website, address, source, source_url,
social_links, metadata (page_title, emails_found, phones_found,
pages_crawled, depth, ...), scraped_at.

## Checkpoint / resume
Cursor = crawl frontier (pending URLs) + pages_fetched. Pause/crash resumes
from the frontier without re-crawling already-seen pages (§18, §25).

## Known limitations
- DNS rebinding is out of scope: addresses are validated at request time; run
  workers without internal network access for hard guarantees (netguard doc).
- JavaScript-rendered contact widgets are not seen by the HTTP-only crawler
  (Playwright arrives in a later phase behind the same actor contract).
- Single-page applications may expose their contact info only via JS.
