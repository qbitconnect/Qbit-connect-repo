"""PublicDataActor — open/public dataset ingestion (system diagram §2).

Fetches a public JSON array (or CSV) endpoint and streams rows through the
normalizer. Typical use: open-data portals, government business registers,
public charity/company lists. Fully declarative via `field_map`
(source column → canonical lead field); unmapped columns ride in metadata.
"""

from __future__ import annotations

import csv
import io
import json

from app.scrapers.actors.public_data.schemas import OUTPUT_FIELDS, PublicDataInput
from app.scrapers.core.base import ActorCategory, ScraperActor
from app.scrapers.core.exceptions import (
    ScraperLimitReachedError,
    ScraperNetworkError,
    ScraperValidationError,
)
from app.scrapers.core.netguard import validate_url_async

_CANONICAL = (
    "business_name", "contact_name", "email", "phone", "website", "address",
    "city", "state", "country", "category", "rating", "review_count",
)


class PublicDataActor(ScraperActor):
    id = "public-data"
    name = "Public Data"
    version = "1.0.0"
    description = (
        "Ingest rows from a public open-data endpoint (JSON array/object or "
        "CSV) with a declarative field mapping. Ideal for government registers "
        "and open datasets."
    )
    category = ActorCategory.PUBLIC_DATA
    author = "QBIT"
    capabilities = (
        "public JSON datasets",
        "public CSV datasets",
        "declarative field mapping",
        "streaming row processing",
    )
    supports_pause = True
    input_schema = PublicDataInput
    output_fields = OUTPUT_FIELDS

    async def run(self, ctx):
        inp = PublicDataInput.model_validate(ctx.input)
        url = await validate_url_async(str(inp.url), ctx.url_policy)

        await ctx.check_stopped()
        ctx.check_deadline()
        ctx.progress.set_stage("downloading dataset")
        try:
            if inp.format == "csv":
                resp = await ctx.http.get(url)
                if resp.status_code >= 400:
                    raise ScraperNetworkError(f"HTTP {resp.status_code} for {url}")
                rows = _iter_csv(resp.text)
            else:
                payload = await ctx.http.get_json(url)
                rows = _iter_json(payload, inp.records_key)
        except ScraperNetworkError:
            raise
        except (ValueError, KeyError) as exc:
            raise ScraperValidationError(f"Dataset could not be parsed: {exc}")

        yielded = 0
        for row in rows:
            await ctx.check_stopped()
            ctx.check_deadline()
            if not isinstance(row, dict):
                continue
            item = _map_row(row, inp.field_map)
            if not item:
                continue
            item["source"] = self.id
            item.setdefault("source_url", url)
            yield item
            yielded += 1
            ctx.progress.set_stage(f"ingesting rows ({yielded})")
            await ctx.save_checkpoint({"rows_yielded": yielded})
            if yielded >= inp.max_records:
                raise ScraperLimitReachedError(f"max_records limit reached ({inp.max_records})")

        await ctx.save_checkpoint({"rows_yielded": yielded}, force=True)

    async def cleanup(self, ctx) -> None:
        await ctx.close()


def _map_row(row: dict, field_map: dict[str, str]) -> dict | None:
    item: dict = {}
    metadata: dict = {}
    for key, value in row.items():
        mapped = field_map.get(key, key if key in _CANONICAL else None)
        if value is None or value == "":
            continue
        if mapped in _CANONICAL:
            item[mapped] = value
        else:
            metadata[key] = value
    if metadata:
        item["metadata"] = metadata
    return item or None


def _iter_json(payload, records_key: str | None):
    if records_key:
        for part in records_key.split("."):
            payload = payload[part]  # KeyError → clear validation error upstream
    if isinstance(payload, dict):
        raise ValueError("JSON payload is an object; provide records_key pointing to an array")
    if not isinstance(payload, list):
        raise ValueError("JSON payload must be an array of records")
    return iter(payload)


def _iter_csv(text: str):
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        yield dict(row)
