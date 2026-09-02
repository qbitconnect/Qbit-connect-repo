"""Google Maps actor schemas (brief §8, §28)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class GoogleMapsInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    max_results: int = Field(default=50, ge=1, le=5000)
    language: str | None = Field(default=None, max_length=10)

    @field_validator("query")
    @classmethod
    def _clean_query(cls, v: str) -> str:
        return v.strip()


OUTPUT_FIELDS = (
    "business_name",
    "category",
    "phone",
    "email",
    "website",
    "address",
    "city",
    "state",
    "country",
    "source",
    "source_url",
    "rating",
    "review_count",
    "metadata",
    "scraped_at",
)
