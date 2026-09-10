"""JustDial Lead actor schemas (spec §7.D)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, field_validator


class JustDialMode(str, Enum):
    SEARCH = "search"          # category + city (spec §7.D mode 1)
    SEARCH_URL = "search_url"  # pasted search/listing URL (mode 2)
    BUSINESS_URL = "business_url"  # single business page (mode 3)
    BULK_URLS = "bulk_urls"    # mode 4


class JustDialInput(BaseModel):
    mode: JustDialMode = JustDialMode.SEARCH
    city: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=150)
    keyword: str | None = Field(default=None, max_length=150)
    search_url: HttpUrl | None = None
    urls: list[HttpUrl] = Field(default_factory=list, max_length=50)
    max_pages: int = Field(default=1, ge=1, le=20)
    max_results: int = Field(default=60, ge=1, le=1000)
    include_details: bool = False
    include_contact_info: bool = True
    request_timeout: int = Field(default=20, ge=1, le=120)
    respect_robots: bool = True

    @field_validator("urls")
    @classmethod
    def _justdial_only(cls, v: list[HttpUrl]) -> list[HttpUrl]:
        for item in v:
            if "justdial.com" not in str(item):
                raise ValueError(f"only justdial.com URLs are supported: {item}")
        return v

    def validate_policy(self) -> dict[str, str]:
        errors: dict[str, str] = {}
        if self.mode == JustDialMode.SEARCH and not (self.category and self.city):
            errors["search"] = "category and city are required for search mode"
        if self.mode == JustDialMode.SEARCH_URL and not self.search_url:
            errors["search_url"] = "search_url is required for search_url mode"
        if self.mode == JustDialMode.BUSINESS_URL and not self.urls:
            errors["urls"] = "one business URL in urls is required"
        if self.mode == JustDialMode.BULK_URLS and not self.urls:
            errors["urls"] = "urls are required for bulk_urls mode"
        return errors


OUTPUT_FIELDS = (
    "business_name",
    "phone",
    "email",
    "website",
    "address",
    "city",
    "rating",
    "review_count",
    "source",
    "source_url",
    "metadata",
    "scraped_at",
)
