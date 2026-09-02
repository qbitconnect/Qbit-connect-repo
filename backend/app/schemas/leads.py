"""Lead workspace API schemas (Phase 4 §30)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LeadCreate(BaseModel):
    business_name: str | None = Field(default=None, max_length=300)
    contact_name: str | None = Field(default=None, max_length=300)
    first_name: str | None = Field(default=None, max_length=150)
    last_name: str | None = Field(default=None, max_length=150)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    website: str | None = Field(default=None, max_length=500)
    address: str | None = Field(default=None, max_length=500)
    city: str | None = Field(default=None, max_length=150)
    state: str | None = Field(default=None, max_length=150)
    postal_code: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=150)
    category: str | None = Field(default=None, max_length=150)
    industry: str | None = Field(default=None, max_length=150)
    source_id: str | None = Field(default=None, max_length=300)
    source_url: str | None = Field(default=None, max_length=1000)
    status: str | None = Field(default=None, max_length=30)
    tags: list[str] | None = None


class LeadUpdate(LeadCreate):
    pass


class LeadListQuery(BaseModel):
    search: str | None = None
    filters: dict[str, Any] | list[Any] | None = None
    sort: str | None = None
    page: int = 1
    page_size: int = 25


class BulkActionRequest(BaseModel):
    action: str = Field(..., pattern=r"^(set_status|archive|restore|add_tag|remove_tag|delete|export)$")
    lead_ids: list[str] = Field(..., min_length=1)
    params: dict[str, Any] | None = None


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=20)


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=20)


class LeadTagAssignRequest(BaseModel):
    tags: list[str] = Field(..., min_length=1)


class NoteCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=20000)


class StatusChangeRequest(BaseModel):
    status: str = Field(..., min_length=2, max_length=30)


class MergeRequest(BaseModel):
    primary_lead_id: str


class DuplicateResolveRequest(BaseModel):
    action: str = Field(..., pattern=r"^(keep_both|ignore)$")
    note: str | None = Field(default=None, max_length=500)


class SavedViewCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    filters: dict[str, Any] | list[Any]
    visibility: str = Field(default="PRIVATE", pattern=r"^(PRIVATE|TEAM|GLOBAL)$")


class SavedViewUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    filters: dict[str, Any] | list[Any] | None = None


class ExportRequest(BaseModel):
    format: str = Field(..., pattern=r"^(csv|xlsx|json|jsonl)$")
    scope: str = Field(default="filtered", pattern=r"^(selected|filtered|page|all|lead)$")
    filters: dict[str, Any] | list[Any] | None = None
    search: str | None = Field(default=None, max_length=200)
    sort: str | None = Field(default=None, max_length=200)
    ids: list[str] | None = None
    lead_id: str | None = None
    fields: list[str] | None = None
    page: int = 1
    page_size: int = 100


class ImportMappingRequest(BaseModel):
    mapping: dict[str, str]
    duplicate_strategy: str = Field(
        default="SKIP_DUPLICATES",
        pattern=r"^(SKIP_DUPLICATES|UPDATE_EXISTING|CREATE_NEW|REVIEW)$",
    )
    default_status: str = Field(default="NEW", max_length=30)
    tags: list[str] | None = None
    source_name: str | None = Field(default=None, max_length=100)
