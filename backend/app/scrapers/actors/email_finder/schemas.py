"""Email Finder actor schemas (brief §8, §27)."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class EmailFinderInput(BaseModel):
    """Discover publicly displayed business contact emails."""

    website: HttpUrl | None = None
    domain: str | None = None
    crawl_depth: int = Field(default=2, ge=0, le=4)
    max_pages: int = Field(default=15, ge=1, le=200)
    request_timeout: int = Field(default=20, ge=1, le=120)
    respect_robots: bool = True

    @field_validator("domain")
    @classmethod
    def _domain_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip().lower().removeprefix("http://").removeprefix("https://").rstrip("/")
        if "/" in value or "@" in value or not value:
            raise ValueError("domain must be a bare hostname like example.com")
        return value

    @model_validator(mode="after")
    def _need_target(self) -> "EmailFinderInput":
        if self.website is None and self.domain is None:
            raise ValueError("Either 'website' (URL) or 'domain' (hostname) is required")
        return self


#: Email classification buckets (brief §27).
EMAIL_TYPES = ("general", "sales", "support", "info", "contact", "other")
OUTPUT_FIELDS = (
    "business_name",
    "email",
    "website",
    "source",
    "source_url",
    "metadata",
    "scraped_at",
)
