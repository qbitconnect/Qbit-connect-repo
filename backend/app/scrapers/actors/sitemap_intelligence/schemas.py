"""Sitemap intelligence input/output schemas (spec §18)."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, field_validator


class SitemapInput(BaseModel):
    """Strict input schema — invalid input never reaches the worker (§8)."""

    url: HttpUrl
    max_urls_sampled: int = Field(default=10, ge=1, le=50)
    extract_structured_data: bool = True
    request_timeout: int = Field(default=20, ge=1, le=120)

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme not in ("http", "https"):
            raise ValueError("Only http/https URLs are supported")
        return v


#: Normalized lead fields this actor fills (spec §9 core fields subset).
OUTPUT_FIELDS = (
    "business_name",
    "website",
    "source",
    "source_url",
    "metadata",
)
