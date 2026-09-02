# Universal Web Actor (`universal-web` v1.0.0)

Controlled, configurable extraction foundation (brief §30).

## Philosophy
Deterministic, not magical: the user declares exactly what to extract.
- `fields`: list of {name, selector, attribute} rules (attribute default `text`).
- `item_selector`: optional CSS selector for repeating list items; when absent,
  fields are read from the page root.
- `pagination_next_selector` + `max_pages` (≤50): optional pagination; hops are
  same-domain unless `follow_same_domain=true` (§42).
- Every URL passes SSRF validation; robots.txt respected; cooperative
  pause/cancel; checkpoint = frontier + pages fetched.

## Input example
```json
{
  "url": "https://directory.example.com/listing",
  "item_selector": "div.company-card",
  "fields": [
    {"name": "business_name", "selector": "h2.title", "attribute": "text"},
    {"name": "phone", "selector": "a.tel", "attribute": "href"},
    {"name": "email", "selector": "a.mailto", "attribute": "href"}
  ],
  "max_pages": 5
}
```

## Output
Whatever the declared fields capture (arbitrary names land in metadata via the
normalizer; canonical names — business_name, phone, email, website, address,
city, state, country, category, rating, review_count — fill lead fields),
plus source/source_url/scraped_at provenance.
