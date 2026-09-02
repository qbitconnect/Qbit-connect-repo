"""Business Directory actor schemas (brief §29)."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class DirectoryAdapterConfig(BaseModel):
    """Declarative configuration for the generic directory adapter.

    Deterministic CSS-selector extraction — no site-specific Python code gets
    hardcoded into the actor (§29: source adapters, not one giant class).
    """

    list_url: HttpUrl                      # directory listing page to fetch
    item_selector: str = Field(min_length=1, max_length=200)
    fields: dict[str, str]                 # lead field -> CSS selector (relative)
    field_attributes: dict[str, str] = {}  # lead field -> attribute (default: text)
    pagination_next_selector: str | None = Field(default=None, max_length=200)
    max_list_pages: int = Field(default=1, ge=1, le=50)


class BusinessDirectoryInput(BaseModel):
    adapter: str = Field(default="generic", max_length=50)
    config: DirectoryAdapterConfig
    max_results: int = Field(default=200, ge=1, le=10000)
    request_timeout: int = Field(default=20, ge=1, le=120)
    respect_robots: bool = True


OUTPUT_FIELDS = (
    "business_name",
    "phone",
    "email",
    "website",
    "address",
    "city",
    "state",
    "country",
    "category",
    "source",
    "source_url",
    "metadata",
    "scraped_at",
)
