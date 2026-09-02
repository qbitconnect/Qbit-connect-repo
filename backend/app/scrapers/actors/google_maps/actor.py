"""GoogleMapsActor — business listing extraction via pluggable provider.

The actor contains NO scraping logic against Google and no evasion of any
platform control (brief §28). It consumes a MapsProvider (provider.py): an
operator-configured compliant data endpoint, or the mock provider for tests.
Without a configured provider the actor is DEGRADED and refuses to run with a
clear configuration error (§49) — it never fakes success (§55).

Checkpoint cursor = provider page_token + records yielded so far, so pause /
crash resume resumes pagination (§18).
"""

from __future__ import annotations

from app.scrapers.actors.google_maps.provider import build_maps_provider
from app.scrapers.actors.google_maps.schemas import OUTPUT_FIELDS, GoogleMapsInput
from app.scrapers.core.base import ActorCategory, ActorHealth, ActorStatus, ScraperActor
from app.scrapers.core.exceptions import ScraperConfigurationError


class GoogleMapsActor(ScraperActor):
    id = "google-maps"
    name = "Google Maps"
    version = "1.0.0"
    description = (
        "Business listing extraction (name, category, phone, website, "
        "address, rating) through a configurable compliant data provider. "
        "No CAPTCHA bypass, no anti-bot evasion — by design."
    )
    category = ActorCategory.BUSINESS_LEADS
    author = "QBIT"
    capabilities = (
        "business listing fields",
        "provider-based (configurable)",
        "pagination checkpoints",
        "compliant access only",
    )
    supports_pause = True
    input_schema = GoogleMapsInput
    output_fields = OUTPUT_FIELDS

    def __init__(self, settings=None) -> None:
        self._settings = settings

    async def health_check(self) -> ActorHealth:
        if self._settings is None:
            return ActorHealth(status=ActorStatus.READY, detail="No settings wired (tests)")
        try:
            provider = build_maps_provider(self._settings)
        except Exception as exc:  # noqa: BLE001 — configuration problems surface
            return ActorHealth(status=ActorStatus.FAILED, detail=str(exc))
        if provider is None:
            return ActorHealth(
                status=ActorStatus.DEGRADED,
                detail="No maps provider configured (QBIT_MAPS_PROVIDER=none); "
                       "configure a compliant provider to enable this actor.",
                dependencies={"maps_provider": "missing"},
            )
        return ActorHealth(
            status=ActorStatus.READY,
            detail=f"provider={provider.name}",
            dependencies={"maps_provider": provider.name},
        )

    async def run(self, ctx):
        inp = GoogleMapsInput.model_validate(ctx.input)
        if self._settings is None:
            raise ScraperConfigurationError(
                "google-maps actor requires settings (provider configuration)"
            )
        provider = build_maps_provider(self._settings)
        if provider is None:
            raise ScraperConfigurationError(
                "No maps provider configured. Set QBIT_MAPS_PROVIDER=http plus "
                "QBIT_MAPS_PROVIDER_URL (a compliant data endpoint). Direct "
                "scraping/evasion of Google is not supported by design."
            )

        page_token: str | None = None
        if ctx.checkpoint and ctx.checkpoint.data.get("page_token"):
            page_token = ctx.checkpoint.data["page_token"]
        elif ctx.checkpoint:
            ctx.checkpoint_cursor({"page_token": None})

        yielded = 0
        while yielded < inp.max_results:
            await ctx.check_stopped()
            ctx.check_deadline()
            ctx.progress.set_stage(f"fetching provider page (token={page_token})")
            raw_items, next_token = await provider.search(
                query=inp.query,
                city=inp.city,
                state=inp.state,
                country=inp.country,
                language=inp.language,
                page_token=page_token,
                max_results=inp.max_results - yielded,
                http=ctx.http,
            )
            if not raw_items:
                break
            for raw in raw_items:
                await ctx.check_stopped()
                raw["source"] = self.id
                if not raw.get("source_url"):
                    raw["source_url"] = (
                        f"https://www.google.com/maps/search/{inp.query.replace(' ', '+')}"
                    )
                yield raw
                yielded += 1
                if yielded >= inp.max_results:
                    break
            await ctx.save_checkpoint({"page_token": next_token, "yielded": yielded})
            ctx.progress.set_stage(f"provider pages done ({yielded} records)")
            if next_token is None:
                break
            page_token = next_token

        await ctx.save_checkpoint({"page_token": None, "yielded": yielded}, force=True)

    async def cleanup(self, ctx) -> None:
        await ctx.close()
