# Business Directory Actor (`business-directory` v1.0.0)

Generic directory-scraper architecture (brief §29) — **no hardcoded websites**.

## Adapter architecture
    BusinessDirectoryActor
        └── DirectorySource adapters (adapters.py)
              ├── generic      — declarative CSS selectors from job input
              └── (add yours)  — implement DirectorySource, register in
                                 ADAPTER_REGISTRY (one line)

Each adapter declares: source_name, how to enumerate listing pages (`pages`),
how to parse entries (`parse`) and how to follow pagination (`next_page`).

## The `generic` adapter (shipped)
Operator supplies in the job input (validated against DirectoryAdapterConfig):
- `list_url` — the directory listing page
- `item_selector` — CSS selector matching one directory entry
- `fields` — lead field → relative CSS selector
- `field_attributes` — optional attribute to read (default: text)
- `pagination_next_selector` — optional "next page" link selector
- `max_list_pages` — hard pagination bound (≤50)

## Input
adapter ("generic"), config (above), max_results (≤10000), request_timeout,
respect_robots.

## Output
business_name, phone, email, website, address, city, state, country, category,
source, source_url, metadata, scraped_at — whatever the selectors capture.

## Safety
Every listing URL passes netguard SSRF validation; robots.txt respected;
bounded pages/items; cooperative pause/cancel between entries; checkpoint =
current listing URL + yielded count.
