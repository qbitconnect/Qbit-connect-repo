"""QBIT CONNECT — Scraper Tool Registry (Brief §8, §26).

Wraps all existing built-in specialized scrapers into a machine-readable,
declarative capability catalog without duplicating or modifying their
underlying execution engines.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.scrapers.core.base import ActorCategory, ActorHealth, ActorStatus, ScraperActor
from app.services.scraping.registry import ActorRegistry


@dataclass(frozen=True)
class ToolCapabilityDefinition:
    actor_id: str
    name: str
    source_name: str
    category: str
    description: str
    capabilities: list[str]
    target_entities: list[str]
    supported_modes: list[str]
    input_schema_summary: dict[str, Any]
    output_fields: list[str]
    supports_pagination: bool = True
    supports_pause: bool = True
    requires_browser: bool = False
    requires_external_provider: bool = False
    compliance_notes: str = "Public data only; respects robots.txt; no anti-bot or CAPTCHA evasion."

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Canonical capability catalog mapping actor_id -> metadata
TOOL_CATALOG_DEFINITIONS: dict[str, dict[str, Any]] = {
    "indiamart": {
        "source_name": "IndiaMART",
        "target_entities": ["supplier", "product", "business_lead", "wholesale"],
        "supported_modes": ["product_search", "supplier_search", "urls"],
        "compliance_notes": "Public supplier & product directory pages; publicly exposed contact details only.",
    },
    "justdial": {
        "source_name": "JustDial",
        "target_entities": ["local_business", "business_lead", "store", "vendor", "directory_lead"],
        "supported_modes": ["category_city", "url", "bulk_urls"],
        "compliance_notes": "Public business listings; rendered phone glyph decoding; respects robots.txt.",
    },
    "google-maps": {
        "source_name": "Google Maps",
        "target_entities": ["local_business", "business_lead", "poi", "shop", "restaurant", "clinic"],
        "supported_modes": ["query_location"],
        "requires_external_provider": True,
        "compliance_notes": "Configurable compliant maps provider only; no scraping/evasion against Google.",
    },
    "instagram": {
        "source_name": "Instagram",
        "target_entities": ["social_profile", "influencer", "brand", "hashtag_posts"],
        "supported_modes": ["profile", "posts", "hashtag", "search"],
        "compliance_notes": "Logged-out public pages only; login walls reported honestly as blocked.",
    },
    "linkedin-public": {
        "source_name": "LinkedIn",
        "target_entities": ["company", "public_profile"],
        "supported_modes": ["company", "profile", "slug"],
        "compliance_notes": "Public logged-out pages only; authwalls reported honestly as blocked.",
    },
    "meta-ads-library": {
        "source_name": "Meta Ads Library",
        "target_entities": ["ad_creative", "advertiser", "campaign_intelligence"],
        "supported_modes": ["keyword_search", "page_search", "url"],
        "compliance_notes": "Public Meta Ad Library archives; content hash change detection.",
    },
    "email-finder": {
        "source_name": "Domain Email Finder",
        "target_entities": ["business_email", "contact"],
        "supported_modes": ["domain_crawl"],
        "compliance_notes": "BFS crawl of public contact pages; classified by type (sales/support/info).",
    },
    "business-directory": {
        "source_name": "Custom Business Directory",
        "target_entities": ["business_lead", "directory_entry"],
        "supported_modes": ["adapter_config"],
        "compliance_notes": "Declarative CSS selectors over operator-configured directory sites.",
    },
    "public-data": {
        "source_name": "Public Data Ingestion",
        "target_entities": ["open_dataset", "government_registry", "company_records"],
        "supported_modes": ["json_endpoint", "csv_endpoint"],
        "compliance_notes": "Open public data endpoints with declarative field mapping.",
    },
    "sitemap-intelligence": {
        "source_name": "Sitemap Intelligence",
        "target_entities": ["sitemap_entry", "url_inventory", "structured_data"],
        "supported_modes": ["domain_audit"],
        "compliance_notes": "robots.txt discovery, XML sitemaps, JSON-LD and OpenGraph inspection.",
    },
    "website": {
        "source_name": "Website Crawler",
        "target_entities": ["webpage", "contact_info", "social_links"],
        "supported_modes": ["single_domain_crawl"],
        "compliance_notes": "Same-domain BFS crawl; extracts public emails, phones, and social links.",
    },
    "universal-web": {
        "source_name": "Universal Web Scraper",
        "target_entities": ["generic_webpage", "auto_extract"],
        "supported_modes": ["selectors", "auto"],
        "requires_browser": False,  # Optional level-5 Playwright fallback if available
        "compliance_notes": "Layered extraction: JSON-LD, meta, tables, contacts; transparent limitations.",
    },
}

# Colloquial alias index for flexible intent mapping
SOURCE_ALIASES: dict[str, str] = {
    "indiamart": "indiamart",
    "india mart": "indiamart",
    "b2b": "indiamart",
    "suppliers": "indiamart",
    "justdial": "justdial",
    "just dial": "justdial",
    "jd": "justdial",
    "google maps": "google-maps",
    "google-maps": "google-maps",
    "gmaps": "google-maps",
    "maps": "google-maps",
    "instagram": "instagram",
    "insta": "instagram",
    "ig": "instagram",
    "linkedin": "linkedin-public",
    "linkedin-public": "linkedin-public",
    "meta ads": "meta-ads-library",
    "meta-ads": "meta-ads-library",
    "meta-ads-library": "meta-ads-library",
    "facebook ads": "meta-ads-library",
    "fb ads": "meta-ads-library",
    "ad library": "meta-ads-library",
    "email": "email-finder",
    "email finder": "email-finder",
    "email-finder": "email-finder",
    "emails": "email-finder",
    "business directory": "business-directory",
    "business-directory": "business-directory",
    "directory": "business-directory",
    "public data": "public-data",
    "public-data": "public-data",
    "open data": "public-data",
    "sitemap": "sitemap-intelligence",
    "sitemap-intelligence": "sitemap-intelligence",
    "website": "website",
    "web": "website",
    "universal": "universal-web",
    "universal web": "universal-web",
    "universal-web": "universal-web",
}


class ToolRegistry:
    """High-level semantic tool registry for agent orchestration."""

    def __init__(self, actor_registry: ActorRegistry | None = None) -> None:
        self._actor_registry = actor_registry or ActorRegistry()
        self._definitions: dict[str, ToolCapabilityDefinition] = {}
        self._rebuild_definitions()

    def _rebuild_definitions(self) -> None:
        self._definitions.clear()
        for actor_id in self._actor_registry.discover():
            entry = self._actor_registry.entry(actor_id)
            if entry is None:
                continue
            actor: ScraperActor = entry.actor
            meta = actor.metadata()

            catalog_meta = TOOL_CATALOG_DEFINITIONS.get(actor_id, {})
            source_name = catalog_meta.get("source_name", meta["name"])
            target_entities = catalog_meta.get("target_entities", ["generic"])
            supported_modes = catalog_meta.get("supported_modes", ["default"])
            compliance = catalog_meta.get("compliance_notes", "Public data only.")
            req_provider = catalog_meta.get("requires_external_provider", False)
            req_browser = catalog_meta.get("requires_browser", False)

            # Summarize input schema properties
            raw_props = meta.get("input_schema", {}).get("properties", {})
            required_props = set(meta.get("input_schema", {}).get("required", []))
            summary_props = {}
            for pname, pdef in raw_props.items():
                summary_props[pname] = {
                    "type": pdef.get("type", "string"),
                    "required": pname in required_props,
                    "default": pdef.get("default"),
                    "description": pdef.get("description", ""),
                }

            defn = ToolCapabilityDefinition(
                actor_id=actor_id,
                name=meta["name"],
                source_name=source_name,
                category=meta["category"],
                description=meta["description"],
                capabilities=list(meta.get("capabilities", ())),
                target_entities=target_entities,
                supported_modes=supported_modes,
                input_schema_summary=summary_props,
                output_fields=list(actor.output_fields),
                supports_pagination=getattr(actor, "supports_pagination", True),
                supports_pause=getattr(actor, "supports_pause", True),
                requires_browser=req_browser,
                requires_external_provider=req_provider,
                compliance_notes=compliance,
            )
            self._definitions[actor_id] = defn

    def list_tools(self) -> list[ToolCapabilityDefinition]:
        return list(self._definitions.values())

    def get_tool(self, actor_id: str) -> ToolCapabilityDefinition | None:
        return self._definitions.get(actor_id)

    def resolve_source(self, source_hint: str) -> str | None:
        """Resolve colloquial source name to canonical actor_id."""
        if not source_hint:
            return None
        norm = source_hint.lower().strip()
        if norm in self._definitions:
            return norm
        return SOURCE_ALIASES.get(norm)

    def find_tools_by_entity(self, entity: str) -> list[ToolCapabilityDefinition]:
        entity_norm = entity.lower().strip()
        matches = []
        for defn in self._definitions.values():
            if any(entity_norm in te for te in defn.target_entities):
                matches.append(defn)
        return matches

    def find_tools_by_capability(self, capability: str) -> list[ToolCapabilityDefinition]:
        cap_norm = capability.lower().strip()
        matches = []
        for defn in self._definitions.values():
            if any(cap_norm in c.lower() for c in defn.capabilities):
                matches.append(defn)
        return matches

    def get_actor_instance(self, actor_id: str) -> ScraperActor:
        return self._actor_registry.get(actor_id)
