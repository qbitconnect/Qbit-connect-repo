"""Directory source adapters (brief §29).

    BusinessDirectoryActor
        ├── GenericDirectoryAdapter (declarative CSS selectors)
        └── future adapters: implement DirectorySource + register in
            ADAPTER_REGISTRY (one line, no core changes)

Each adapter DECLARES its contract (source_name, base rules, input shape) and
implements `pages()` / `parse()`. Adapters never bypass access controls and
never evade platform restrictions (§17).
"""

from __future__ import annotations

from typing import Iterator, Protocol

from bs4 import BeautifulSoup

from app.scrapers.actors.business_directory.schemas import DirectoryAdapterConfig
from app.scrapers.core.exceptions import ScraperConfigurationError
from app.scrapers.core.netguard import canonical_url, normalize_url

#: canonical lead fields an adapter may fill
ALLOWED_FIELDS = (
    "business_name", "phone", "email", "website", "address", "city",
    "state", "country", "category",
)


class DirectorySource(Protocol):
    source_name: str

    def pages(self, config: DirectoryAdapterConfig) -> Iterator[str]:
        """Yield listing-page URLs (validated by the caller's netguard)."""
        ...

    def parse(
        self, config: DirectoryAdapterConfig, soup: BeautifulSoup, page_url: str
    ) -> list[dict]:
        """Parse one listing page into raw lead dicts."""
        ...


class GenericDirectoryAdapter:
    """Declarative adapter: the operator supplies CSS selectors in the job
    input; no hardcoded websites. Deterministic and inspectable (§30 spirit,
    §29 mechanics)."""

    source_name = "generic"

    def pages(self, config: DirectoryAdapterConfig) -> Iterator[str]:
        yield str(config.list_url)

    def parse(
        self, config: DirectoryAdapterConfig, soup: BeautifulSoup, page_url: str
    ) -> list[dict]:
        items: list[dict] = []
        for node in soup.select(config.item_selector)[:500]:
            record: dict = {}
            for field, selector in config.fields.items():
                if field not in ALLOWED_FIELDS:
                    continue
                element = node.select_one(selector)
                if element is None:
                    continue
                attribute = config.field_attributes.get(field, "text")
                if attribute == "text":
                    value = element.get_text(" ", strip=True)
                else:
                    value = element.get(attribute) or ""
                value = str(value).strip()
                if value:
                    record[field] = value[:1000]
            if record:
                record.setdefault("metadata_page", page_url)
                items.append(record)
        return items

    def next_page(
        self, config: DirectoryAdapterConfig, soup: BeautifulSoup, base_url: str
    ) -> str | None:
        """Resolve the next-page link against the CURRENT page URL (not the
        configured list_url) so relative hrefs keep working on page 2+."""
        if not config.pagination_next_selector:
            return None
        tag = soup.select_one(config.pagination_next_selector)
        if tag is None or not tag.get("href"):
            return None
        return normalize_url(base_url, str(tag["href"]))


ADAPTER_REGISTRY: dict[str, type] = {
    "generic": GenericDirectoryAdapter,
}


def get_adapter(name: str):
    adapter_cls = ADAPTER_REGISTRY.get(name.strip().lower())
    if adapter_cls is None:
        raise ScraperConfigurationError(
            f"Unknown directory adapter {name!r}. Available: {sorted(ADAPTER_REGISTRY)}"
        )
    return adapter_cls()


__all__ = [
    "ADAPTER_REGISTRY",
    "DirectorySource",
    "GenericDirectoryAdapter",
    "canonical_url",
    "get_adapter",
]
