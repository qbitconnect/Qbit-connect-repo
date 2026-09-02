"""Website actor input/output schemas (brief §8, §9, §26)."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, field_validator


class WebsiteInput(BaseModel):
    """Strict input schema — invalid input never reaches the worker (§8)."""

    url: HttpUrl
    max_pages: int = Field(default=20, ge=1, le=500)
    max_depth: int = Field(default=2, ge=0, le=5)
    extract_emails: bool = True
    extract_phones: bool = True
    extract_social_links: bool = True
    extract_text: bool = False
    respect_robots: bool = True
    request_timeout: int = Field(default=20, ge=1, le=120)

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme not in ("http", "https"):
            raise ValueError("Only http/https URLs are supported")
        return v


#: Normalized lead fields this actor fills (brief §9 core fields subset).
OUTPUT_FIELDS = (
    "business_name",
    "email",
    "phone",
    "website",
    "address",
    "city",
    "state",
    "country",
    "category",
    "source",
    "source_url",
    "social_links",
    "metadata",
    "scraped_at",
)
