"""Meta Ads Library actor schemas (spec §7.B)."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class MetaAdsInput(BaseModel):
    #: full Ad Library URL (highest fidelity — the site's own query state)
    ad_library_url: HttpUrl | None = None
    keyword: str | None = Field(default=None, min_length=1, max_length=200)
    country: str = Field(default="IN", max_length=4)
    platform: str | None = Field(default=None, max_length=40)  # facebook/instagram/...
    active_status: str = Field(default="all", max_length=20)  # all/active/inactive
    page_name: str | None = Field(default=None, max_length=200)
    max_ads: int = Field(default=50, ge=1, le=500)
    request_timeout: int = Field(default=20, ge=1, le=120)
    respect_robots: bool = True

    def validate_policy(self) -> dict[str, str]:
        if not self.ad_library_url and not self.keyword and not self.page_name:
            return {"input": "one of ad_library_url, keyword or page_name is required"}
        return {}


OUTPUT_FIELDS = (
    "business_name",
    "website",
    "source",
    "source_url",
    "metadata",
    "scraped_at",
)
