"""LinkedIn public actor schemas (spec §7.C)."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, field_validator


class LinkedInInput(BaseModel):
    mode: str = Field(default="company", max_length=20)  # profile | company
    #: single public URL (profile or company per mode)
    url: HttpUrl | None = None
    #: bulk public URLs (spec §7.C modes 3/4)
    urls: list[HttpUrl] = Field(default_factory=list, max_length=25)
    company_slug: str | None = Field(default=None, max_length=200)
    max_results: int = Field(default=25, ge=1, le=100)
    request_timeout: int = Field(default=20, ge=1, le=120)
    respect_robots: bool = True

    @field_validator("urls")
    @classmethod
    def _linkedin_only(cls, v: list[HttpUrl]) -> list[HttpUrl]:
        for item in v:
            host = str(item).split("/")[2] if "://" in str(item) else ""
            if "linkedin.com" not in host:
                raise ValueError(f"only linkedin.com URLs are supported: {item}")
        return v

    def target_urls(self) -> list[str]:
        if self.url:
            return [str(self.url)]
        if self.urls:
            return [str(u) for u in self.urls]
        if self.company_slug:
            slug = self.company_slug.strip("/")
            return [f"https://www.linkedin.com/company/{slug}/"]
        return []


OUTPUT_FIELDS = (
    "business_name",
    "website",
    "email",
    "source",
    "source_url",
    "metadata",
    "scraped_at",
)
