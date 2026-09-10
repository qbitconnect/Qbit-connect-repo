"""IndiaMART Lead actor schemas (spec §7.E)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, field_validator


class IndiaMartMode(str, Enum):
    PRODUCT_SEARCH = "product_search"   # keyword → products (mode 1)
    SUPPLIER_SEARCH = "supplier_search"  # keyword → suppliers (mode 2)
    URL = "url"                          # supplier/product URL (modes 5/6)
    BULK_URLS = "bulk_urls"              # mode 7


class IndiaMartInput(BaseModel):
    mode: IndiaMartMode = IndiaMartMode.PRODUCT_SEARCH
    keyword: str | None = Field(default=None, min_length=1, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    urls: list[HttpUrl] = Field(default_factory=list, max_length=50)
    max_pages: int = Field(default=1, ge=1, le=20)
    max_results: int = Field(default=60, ge=1, le=1000)
    include_contact_info: bool = True
    request_timeout: int = Field(default=20, ge=1, le=120)
    respect_robots: bool = True

    @field_validator("urls")
    @classmethod
    def _indiamart_only(cls, v: list[HttpUrl]) -> list[HttpUrl]:
        for item in v:
            if "indiamart.com" not in str(item):
                raise ValueError(f"only indiamart.com URLs are supported: {item}")
        return v

    def validate_policy(self) -> dict[str, str]:
        errors: dict[str, str] = {}
        if self.mode in (IndiaMartMode.PRODUCT_SEARCH, IndiaMartMode.SUPPLIER_SEARCH) and not self.keyword:
            errors["keyword"] = "keyword is required for search modes"
        if self.mode in (IndiaMartMode.URL, IndiaMartMode.BULK_URLS) and not self.urls:
            errors["urls"] = "urls are required for url modes"
        return errors


OUTPUT_FIELDS = (
    "business_name",
    "phone",
    "email",
    "website",
    "address",
    "city",
    "source",
    "source_url",
    "metadata",
    "scraped_at",
)
