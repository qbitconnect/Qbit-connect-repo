"""Scraping API tests (brief §51, §52): RBAC enforcement, job lifecycle,
export handoff to the Phase 2 ExportService."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    VIEWER_EMAIL,
    VIEWER_PASSWORD,
)


async def _token(client, email, password) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def api(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        admin = await _token(c, ADMIN_EMAIL, ADMIN_PASSWORD)
        viewer = await _token(c, VIEWER_EMAIL, VIEWER_PASSWORD)
        yield c, {"Authorization": f"Bearer {admin}"}, {"Authorization": f"Bearer {viewer}"}


async def test_list_scrapers_requires_auth(api):
    client, _admin, _viewer = api
    resp = await client.get("/api/v1/scrapers")
    assert resp.status_code == 401


async def test_list_scrapers_admin_ok(api):
    client, admin, _viewer = api
    resp = await client.get("/api/v1/scrapers", headers=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    ids = {a["id"] for a in body["data"]}
    assert {"website", "email-finder", "google-maps"} <= ids
    card = body["data"][0]
    assert card["input_schema"]["properties"]
    assert card["output_fields"]


async def test_scrapers_health_endpoint(api):
    client, admin, _viewer = api
    resp = await client.get("/api/v1/scrapers/health", headers=admin)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["website"]["status"] == "READY"
    assert data["google-maps"]["status"] == "DEGRADED"  # no provider configured


async def test_validate_endpoint_reports_field_errors(api):
    client, admin, _viewer = api
    resp = await client.post(
        "/api/v1/scrapers/website/validate",
        headers=admin,
        json={"input": {"url": "nope"}},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["valid"] is False
    assert "url" in body["errors"]


async def test_create_job_happy_path(api):
    client, admin, _viewer = api
    resp = await client.post(
        "/api/v1/scrapers/website/jobs",
        headers=admin,
        json={"input": {"url": "https://example.com", "max_pages": 3},
              "config": {"max_pages": 3, "respect_robots": True}},
    )
    assert resp.status_code == 200, resp.text
    job = resp.json()["data"]
    assert job["status"] == "QUEUED"
    assert job["actor_id"] == "website"
    assert job["config"]["max_pages"] == 3


async def test_create_job_rejects_invalid_input(api):
    client, admin, _viewer = api
    resp = await client.post(
        "/api/v1/scrapers/website/jobs",
        headers=admin,
        json={"input": {"url": "not-a-url"}},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "url" in body["error"]["details"]["fields"]


async def test_create_job_rejects_unknown_config_keys(api):
    client, admin, _viewer = api
    resp = await client.post(
        "/api/v1/scrapers/website/jobs",
        headers=admin,
        json={"input": {"url": "https://example.com"},
              "config": {"nonsense_key": 1}},
    )
    assert resp.status_code == 422


async def test_disabled_actor_cannot_run(app, api):
    client, admin, _viewer = api
    # disable at the registry level (simulates feature flag, brief §50)
    app.state.scraper_registry.entry("website").enabled = False
    try:
        resp = await client.post(
            "/api/v1/scrapers/website/jobs",
            headers=admin,
            json={"input": {"url": "https://example.com"}},
        )
        assert resp.status_code == 409
    finally:
        app.state.scraper_registry.entry("website").enabled = True


async def test_job_lifecycle_pause_resume_cancel_logs_results(api):
    client, admin, _viewer = api
    create = await client.post(
        "/api/v1/scrapers/website/jobs",
        headers=admin,
        json={"input": {"url": "https://example.com"}},
    )
    job_id = create.json()["data"]["id"]

    # pause (QUEUED → PAUSED)
    paused = await client.post(f"/api/v1/scrape-jobs/{job_id}/pause", headers=admin)
    assert paused.status_code == 200
    assert paused.json()["data"]["status"] == "PAUSED"

    # resume (PAUSED → re-enqueued; stays PAUSED until a worker claims it)
    resumed = await client.post(f"/api/v1/scrape-jobs/{job_id}/resume", headers=admin)
    assert resumed.status_code == 200

    # logs endpoint works
    logs = await client.get(f"/api/v1/scrape-jobs/{job_id}/logs", headers=admin)
    assert logs.status_code == 200
    types = [e["event_type"] for e in logs.json()["data"]]
    assert "JOB_CREATED" in types and "JOB_PAUSED" in types and "JOB_RESUMED" in types

    # results list (empty, job never ran)
    results = await client.get(f"/api/v1/scrape-jobs/{job_id}/results", headers=admin)
    assert results.status_code == 200
    assert results.json()["meta"]["total"] == 0

    # export refused before completion
    export = await client.get(
        f"/api/v1/scrape-jobs/{job_id}/results/export", headers=admin
    )
    assert export.status_code == 422

    # cancel
    cancelled = await client.post(f"/api/v1/scrape-jobs/{job_id}/cancel", headers=admin)
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "CANCELLED"


async def test_export_handoff_after_completion(app, api):
    """Terminal job + leads → export via the Phase 2 ExportService (§37)."""
    client, admin, _viewer = api
    from app.models.scrape import JobStatus, ScrapeJob
    from app.services.leads import LeadService
    from app.services.scraping.normalizer import normalize_item

    db = app.state.db
    job_id = uuid.uuid4()
    async with db.session() as session:
        session.add(
            ScrapeJob(
                id=job_id, actor_id="website", actor_version="1.0.0",
                status=JobStatus.COMPLETED, input={"url": "https://x.example.com"},
            )
        )
        item = normalize_item(
            {"business_name": "Export Biz", "email": "export@biz.example.com",
             "source_url": "https://x.example.com/"},
            source="website",
        )
        await LeadService().create_or_update(
            session, item, actor_id="website", actor_version="1.0.0", job_id=job_id,
        )
        await session.commit()

    for fmt in ("csv", "json", "xlsx"):
        resp = await client.get(
            f"/api/v1/scrape-jobs/{job_id}/results/export",
            headers=admin,
            params={"format": fmt},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["file_id"]
        assert data["download"].startswith("/api/v1/files/")

    # the exported file downloads via the Phase 2 files API
    resp = await client.get(
        f"/api/v1/scrape-jobs/{job_id}/results/export", headers=admin, params={"format": "csv"}
    )
    file_id = resp.json()["data"]["file_id"]
    download = await client.get(f"/api/v1/files/{file_id}/download", headers=admin)
    assert download.status_code == 200
    assert b"business_name" in download.content
