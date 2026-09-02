"""Universal Web actor schemas (brief §30)."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, field_validator


class FieldSpec(BaseModel):
    """One extraction rule: field name + CSS selector + attribute/text."""

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    selector: str = Field(min_length=1, max_length=200)
    attribute: str = Field(default="text", max_length=50)


class UniversalWebInput(BaseModel):
    url: HttpUrl
    item_selector: str | None = Field(default=None, max_length=200)
    fields: list[FieldSpec] = Field(min_length=1, max_length=30)
    max_pages: int = Field(default=1, ge=1, le=50)
    pagination_next_selector: str | None = Field(default=None, max_length=200)
    follow_same_domain: bool = False
    request_timeout: int = Field(default=20, ge=1, le=120)
    respect_robots: bool = True

    @field_validator("fields")
    @classmethod
    def _unique_names(cls, v: list[FieldSpec]) -> list[FieldSpec]:
        names = [f.name for f in v]
        if len(names) != len(set(names)):
            raise ValueError("Field names must be unique")
        return v


OUTPUT_FIELDS = (
    "business_name",
    "email",
    "phone",
    "website",
    "address",
    "source",
    "source_url",
    "metadata",
    "scraped_at",
)
