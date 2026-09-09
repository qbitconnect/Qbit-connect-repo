"""Scraping engine API schemas (Phase 3, brief §51)."""

from __future__ import annotations

from pydantic import BaseModel


class ActorOut(BaseModel):
    """Scraper card/detail payload (brief §7 metadata)."""

    id: str
    name: str
    slug: str
    version: str
    description: str
    category: str
    author: str
    capabilities: list[str]
    supports_pause: bool
    status: str
    status_detail: str | None = None
    dependencies: dict[str, str] = {}
    input_schema: dict
    output_fields: list[str]


class ActorListOut(BaseModel):
    success: bool = True
    data: list[ActorOut]
    meta: dict = {}


class ActorActionOut(BaseModel):
    success: bool = True
    data: ActorOut


class ValidationReportOut(BaseModel):
    success: bool = True
    data: dict


class ScrapeJobOut(BaseModel):
    id: str
    actor_id: str
    actor_version: str
    status: str
    input: dict
    config: dict
    progress: float
    stage: str | None
    records_found: int
    records_saved: int
    records_updated: int
    records_duplicate: int
    records_failed: int
    attempt: int
    resumed_count: int
    stop_requested: str
    error: str | None
    error_code: str | None
    started_at: str | None
    completed_at: str | None
    paused_at: str | None = None
    cancelled_at: str | None = None
    created_at: str | None
    created_by: str | None


class ScrapeJobListOut(BaseModel):
    success: bool = True
    data: list[ScrapeJobOut]
    meta: dict


class ScrapeJobActionOut(BaseModel):
    success: bool = True
    data: ScrapeJobOut


class ScrapeJobEventOut(BaseModel):
    id: str
    event_type: str
    message: str | None
    metadata: dict
    created_at: str


class ScrapeJobEventsOut(BaseModel):
    success: bool = True
    data: list[ScrapeJobEventOut]
    meta: dict = {}


class LeadOut(BaseModel):
    id: str
    business_name: str | None
    contact_name: str | None
    email: str | None
    phone: str | None
    website: str | None
    address: str | None
    city: str | None
    state: str | None
    country: str | None
    category: str | None
    rating: float | None
    review_count: int | None
    social_links: dict
    metadata: dict
    tags: list
    source: str | None
    source_url: str | None
    source_actor_id: str | None
    source_actor_version: str | None
    source_job_id: str | None
    scraped_at: str | None
    seen_count: int
    last_seen_at: str | None
    created_at: str | None


class LeadListOut(BaseModel):
    success: bool = True
    data: list[LeadOut]
    meta: dict


class CreateJobRequest(BaseModel):
    """POST /api/v1/scrapers/{id}/jobs body."""

    input: dict
    config: dict | None = None
    max_attempts: int | None = None


class ValidateJobRequest(BaseModel):
    """POST /api/v1/scrapers/{id}/validate body."""

    input: dict
    config: dict | None = None
