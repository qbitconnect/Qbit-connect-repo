"""Public Data actor schemas — open datasets (JSON/CSV endpoints)."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, field_validator


class PublicDataInput(BaseModel):
    url: HttpUrl                                   # public JSON array / CSV endpoint
    format: str = Field(default="json", pattern=r"^(json|csv)$")
    records_key: str | None = Field(               # for JSON objects: data.records
        default=None, max_length=100
    )
    field_map: dict[str, str] = Field(default_factory=dict)  # source field -> canonical
    max_records: int = Field(default=1000, ge=1, le=100000)
    request_timeout: int = Field(default=30, ge=1, le=120)

    @field_validator("field_map")
    @classmethod
    def _sane_map(cls, v: dict[str, str]) -> dict[str, str]:
        return {str(k).strip()[:100]: str(val).strip()[:100] for k, val in v.items() if str(val).strip()}


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
    "metadata",
    "scraped_at",
)
