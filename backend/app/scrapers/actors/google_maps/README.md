# Google Maps Actor (`google-maps` v1.0.0)

Business listing extraction through a **pluggable compliant provider**
(brief §28, §47).

## Compliance stance (non-negotiable)
- NO direct scraping of Google properties.
- NO CAPTCHA solving, anti-bot evasion, stealth fingerprinting, IP rotation or
  ban bypass — these are forbidden by the platform's design red lines (§17).
- Data is obtained through an operator-configured provider endpoint
  (`QBIT_MAPS_PROVIDER=http`, `QBIT_MAPS_PROVIDER_URL`, API key via env only)
  — e.g. a licensed maps-data vendor with its own terms.
- Without a configured provider the actor reports **DEGRADED** health and
  refuses to run with a clear configuration error. It never fakes results.

## Providers (§47)
| provider | class | purpose |
|---|---|---|
| `none` | — | default; actor DEGRADED |
| `outscraper` | `OutscraperMapsProvider` | Outscraper Google Maps API v3 compliant provider |
| `http` | `HttpMapsProvider` | operator's compliant data endpoint |
| `mock` | `MockMapsProvider` | deterministic fixtures — tests/dev ONLY, refused in production |

Adding a provider = implement the `MapsProvider` protocol (search →
(list[dict], next_page_token)) in this package. The core engine is untouched.

## Input
query (required), city, state, country, max_results (1–5000), language.

## Output
business_name, category, phone, email, website, address, city, state, country,
rating, review_count, source, source_url, metadata, scraped_at.

## Checkpoint / resume
Cursor = provider `page_token` + records yielded. Paused jobs resume
pagination where they stopped (§18).
