"""Universal Web actor schemas (brief §30; extended by Actor Platform §5).

`fields`-driven deterministic extraction remains the core. NEW in the Actor
Platform: `strategy: auto|selectors` — auto mode runs the layered engine
(HTTP → HTML → JSON-LD/embedded JSON/meta → contacts → links/tables) and
emits a best-effort record WITHOUT any declared fields.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, field_validator


class ExtractStrategy(str, Enum):
    AUTO = "auto"          # layered auto-detect (spec §5)
    SELECTORS = "selectors"  # classic declared fields (brief §30)


class FieldSpec(BaseModel):
    """One extraction rule: field name + CSS selector + attribute/text."""

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    selector: str = Field(min_length=1, max_length=200)
    attribute: str = Field(default="text", max_length=50)


class UniversalWebInput(BaseModel):
    url: HttpUrl
    strategy: ExtractStrategy = ExtractStrategy.SELECTORS
    item_selector: str | None = Field(default=None, max_length=200)
    fields: list[FieldSpec] = Field(default_factory=list, max_length=30)
    max_pages: int = Field(default=1, ge=1, le=50)
    pagination_next_selector: str | None = Field(default=None, max_length=200)
    follow_same_domain: bool = False
    request_timeout: int = Field(default=20, ge=1, le=120)
    respect_robots: bool = True

    @field_validator("fields")
    @classmethod
    def _fields_required_for_selectors(cls, v: list[FieldSpec], info) -> list[FieldSpec]:
        strategy = info.data.get("strategy", ExtractStrategy.SELECTORS)
        if strategy == ExtractStrategy.SELECTORS and not v:
            raise ValueError("fields are required for the selectors strategy")
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
