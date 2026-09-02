# Public Data Actor (`public-data` v1.0.0)

Open/public dataset ingestion (engine diagram, brief §2).

## What it does
- Fetches a **public** JSON array/object or CSV endpoint (netguard-validated).
- Streams rows through the result pipeline (validation → normalization →
  dedup → leads) with bounded memory (§25, §45).
- Declarative `field_map`: source column → canonical lead field; unmapped
  columns are preserved in `metadata`.
- Bounded by `max_records` (≤100 000), request timeout, cooperative controls.

## Input
| field | type | default |
|---|---|---|
| url | URL | required (public JSON/CSV) |
| format | json\\|csv | json |
| records_key | dotted path | for JSON objects, e.g. `data.records` |
| field_map | object | {"org_name": "business_name", "tel": "phone", ...} |
| max_records | int ≤100000 | 1000 |
| request_timeout | int 1–120 | 30 |

## Output
Canonical lead fields per `field_map` + metadata + source/source_url/scraped_at.

## Use cases
Government business registers, open-data portals, public company lists —
anywhere a structured public download exists. Not a crawler; one endpoint,
streamed.
