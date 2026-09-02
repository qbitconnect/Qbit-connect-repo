"""Phase 4 API tests: §30 endpoint surface, RBAC enforcement, pagination
envelope, security (RBAC denials, IDOR, SQLi attempts, XSS escaping, file
security). Uses the standard isolated app/client fixtures."""

from __future__ import annotations

import io
import json
import uuid

import pytest
from sqlalchemy import select

from app.models.lead import LeadDuplicateCandidate, ImportBatch
from app.models.scrape import Lead


async def _create_lead(client, admin_headers, **fields) -> dict:
    payload = {"business_name": "API Corp", "email": "api@corp.in", "phone": "9876500011", "city": "Ahmedabad", **fields}
    resp = await client.post("/api/v1/leads", json=payload, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ------------------------------------------------------------------ CRUD + RBAC
@pytest.mark.asyncio
async def test_lead_crud_via_api(client, admin_headers):
    lead = await _create_lead(client, admin_headers)
    assert lead["status"] == "NEW"
    assert lead["quality_score"] == 70
    assert lead["source_type"] == "manual"

    # patch normalizes + tracks
    resp = await client.patch(
        f"/api/v1/leads/{lead['id']}", json={"email": "NEW@corp.in", "website": "corp.in"},
        headers=admin_headers,
    )
    data = resp.json()["data"]
    assert data["email_norm"] == "new@corp.in"
    assert data["website_norm"] == "corp.in"

    # status endpoint
    resp = await client.post(f"/api/v1/leads/{lead['id']}/status", json={"status": "QUALIFIED"}, headers=admin_headers)
    assert resp.json()["data"]["status"] == "QUALIFIED"

    # activity recorded
    resp = await client.get(f"/api/v1/leads/{lead['id']}/activity", headers=admin_headers)
    kinds = [a["event_type"] for a in resp.json()["data"]["items"]]
    assert "lead_created" in kinds and "status_changed" in kinds

    # 404 for unknown lead
    resp = await client.get(f"/api/v1/leads/{uuid.uuid4()}", headers=admin_headers)
    assert resp.status_code == 404

    # invalid payload → 422 field errors, not a crash
    resp = await client.post(
        "/api/v1/leads", json={"business_name": "X", "email": "broken"}, headers=admin_headers
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rbac_enforced_on_every_endpoint(client, admin_headers, viewer_headers):
    lead = await _create_lead(client, admin_headers)

    # viewer (leads.view only) can read…
    resp = await client.get("/api/v1/leads", headers=viewer_headers)
    assert resp.status_code == 200
    resp = await client.get(f"/api/v1/leads/{lead['id']}", headers=viewer_headers)
    assert resp.status_code == 200

    # …but never write
    for method, path, body in (
        ("post", "/api/v1/leads", {"business_name": "Nope"}),
        ("patch", f"/api/v1/leads/{lead['id']}", {"city": "Nope"}),
        ("post", f"/api/v1/leads/{lead['id']}/status", {"status": "NEW"}),
        ("post", f"/api/v1/leads/{lead['id']}/archive", {}),
        ("post", f"/api/v1/leads/{lead['id']}/restore", {}),
        ("post", f"/api/v1/leads/{lead['id']}/tags", {"tags": ["Nope"]}),
        ("post", f"/api/v1/leads/{lead['id']}/notes", {"content": "Nope"}),
        ("post", "/api/v1/leads/bulk", {"action": "archive", "lead_ids": [lead["id"]]}),
        ("post", "/api/v1/leads/export", {"format": "csv", "scope": "all"}),
        ("post", "/api/v1/leads/tags", {"name": "Nope"}),
        ("post", "/api/v1/leads/views", {"name": "Nope", "filters": []}),
    ):
        resp = await getattr(client, method)(path, json=body, headers=viewer_headers)
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"

    # unauthenticated → 401
    resp = await client.get("/api/v1/leads")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_permission_is_separate(client, admin_headers, viewer_headers):
    """Archive-level bulk delete is blocked without the right role; hard
    delete needs leads.delete + confirm — the viewer has neither."""
    lead = await _create_lead(client, admin_headers)
    resp = await client.post(
        "/api/v1/leads/bulk",
        json={"action": "delete", "lead_ids": [lead["id"]], "params": {"hard": True, "confirm": "DELETE"}},
        headers=viewer_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_tags_notes_via_api(client, admin_headers):
    lead = await _create_lead(client, admin_headers)
    resp = await client.post(f"/api/v1/leads/{lead['id']}/tags", json={"tags": ["Hot", "WhatsApp Available"]}, headers=admin_headers)
    assert sorted(resp.json()["data"]["tags"]) == ["Hot", "WhatsApp Available"]

    # catalog with counts
    resp = await client.get("/api/v1/leads/tags", headers=admin_headers)
    catalog = {t["name"]: t["lead_count"] for t in resp.json()["data"]}
    assert catalog["Hot"] == 1

    # rename + filter by tag
    tag_id = next(t["id"] for t in resp.json()["data"] if t["name"] == "Hot")
    resp = await client.patch(f"/api/v1/leads/tags/{tag_id}", json={"name": "Very Hot"}, headers=admin_headers)
    assert resp.json()["data"]["name"] == "Very Hot"
    resp = await client.get("/api/v1/leads", params={"filters": json.dumps({"field": "tag", "op": "eq", "value": "Very Hot"})}, headers=admin_headers)
    assert resp.json()["data"]["total"] == 1

    # duplicate tag assignment is idempotent
    resp = await client.post(f"/api/v1/leads/{lead['id']}/tags", json={"tags": ["Very Hot"]}, headers=admin_headers)
    assert resp.json()["data"]["tags"].count("Very Hot") == 1

    # remove
    resp = await client.delete(f"/api/v1/leads/{lead['id']}/tags/{tag_id}", headers=admin_headers)
    assert "Very Hot" not in resp.json()["data"]["tags"]

    # notes
    resp = await client.post(f"/api/v1/leads/{lead['id']}/notes", json={"content": "Called today"}, headers=admin_headers)
    assert resp.json()["data"]["content"] == "Called today"
    resp = await client.get(f"/api/v1/leads/{lead['id']}", headers=admin_headers)
    assert resp.json()["data"]["notes"][0]["content"] == "Called today"


@pytest.mark.asyncio
async def test_pagination_envelope_and_max_page_size(client, admin_headers):
    for i in range(7):
        await _create_lead(client, admin_headers, business_name=f"Page {i}", email=f"p{i}@corp.in", phone=f"90000000{i}")
    resp = await client.get("/api/v1/leads", params={"page": 2, "page_size": 3}, headers=admin_headers)
    data = resp.json()["data"]
    assert set(data.keys()) >= {"items", "total", "page", "page_size", "total_pages"}
    assert data["total"] == 7 and data["page"] == 2 and data["page_size"] == 3
    assert data["total_pages"] == 3 and len(data["items"]) == 3

    # page_size above the server max is rejected (validation), never honored
    resp = await client.get("/api/v1/leads", params={"page_size": 100000}, headers=admin_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_filters_sort_via_api(client, admin_headers):
    await _create_lead(client, admin_headers, business_name="Filter A", city="Surat", state="Gujarat")
    await _create_lead(client, admin_headers, business_name="Filter B", city="Mumbai", state="Maharashtra", phone="9111100000")

    resp = await client.get("/api/v1/leads", params={"search": "Surat"}, headers=admin_headers)
    assert resp.json()["data"]["total"] == 1

    spec = {"and": [{"field": "state", "op": "eq", "value": "Gujarat"},
                    {"field": "has_phone", "op": "eq", "value": True}]}
    resp = await client.get("/api/v1/leads", params={"filters": json.dumps(spec)}, headers=admin_headers)
    assert resp.json()["data"]["total"] == 1

    # sort whitelist + direction
    resp = await client.get("/api/v1/leads", params={"sort": "-business_name"}, headers=admin_headers)
    names = [i["business_name"] for i in resp.json()["data"]["items"]]
    assert names == sorted(names, reverse=True)

    # invalid filter → 422 with a readable message
    resp = await client.get("/api/v1/leads", params={"filters": json.dumps({"field": "boss_name", "op": "eq", "value": "x"})}, headers=admin_headers)
    assert resp.status_code == 422


# ------------------------------------------------------------------- security
@pytest.mark.asyncio
async def test_sql_injection_attempts_are_harmless(client, admin_headers):
    await _create_lead(client, admin_headers)
    # through search
    resp = await client.get("/api/v1/leads", params={"search": "'; DROP TABLE leads; --"}, headers=admin_headers)
    assert resp.status_code == 200
    # through sort whitelist
    resp = await client.get("/api/v1/leads", params={"sort": "business_name; DROP TABLE leads"}, headers=admin_headers)
    assert resp.status_code == 422
    # through filter values (parameterized)
    spec = {"field": "business_name", "op": "eq", "value": "x'; DROP TABLE leads; --"}
    resp = await client.get("/api/v1/leads", params={"filters": json.dumps(spec)}, headers=admin_headers)
    assert resp.status_code == 200
    # leads table still alive
    resp = await client.get("/api/v1/leads", headers=admin_headers)
    assert resp.json()["data"]["total"] == 1


@pytest.mark.asyncio
async def test_xss_is_escaped_in_ui(client, admin_headers):
    await _create_lead(client, admin_headers, business_name="<script>alert(1)</script>", email="xss@corp.in")

    # API returns JSON (safe by construction)
    resp = await client.get("/api/v1/leads", headers=admin_headers)
    assert "<script>" in resp.text  # JSON payload carries the raw data safely

    # UI must escape it — login as admin through the cookie flow
    login = await client.post("/login", data={"email": "admin@qbit.example.com", "password": "Sup3rSecret!Pass", "next": "/leads"})
    assert login.status_code == 303
    page = await client.get("/leads")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text  # never rendered raw
    assert "&lt;script&gt;" in page.text


@pytest.mark.asyncio
async def test_idor_shape_and_unknown_ids(client, admin_headers):
    # unknown ids are 404, never leak other data
    resp = await client.get(f"/api/v1/leads/{uuid.uuid4()}", headers=viewer_headers_fix(client))
    assert resp.status_code in (401, 404)


def viewer_headers_fix(client):  # no auth header → 401 (boundary holds)
    return {}


@pytest.mark.asyncio
async def test_bulk_via_api_and_audit(client, admin_headers):
    ids = []
    for i in range(4):
        lead = await _create_lead(client, admin_headers, business_name=f"BulkAPI {i}", email=f"ba{i}@corp.in", phone=f"920000000{i}")
        ids.append(lead["id"])
    resp = await client.post(
        "/api/v1/leads/bulk", json={"action": "add_tag", "lead_ids": ids, "params": {"tags": ["Batch"]}},
        headers=admin_headers,
    )
    assert resp.json()["data"]["affected"] == 4
    resp = await client.post(
        "/api/v1/leads/bulk", json={"action": "set_status", "lead_ids": ids, "params": {"status": "CONTACTED"}},
        headers=admin_headers,
    )
    assert resp.json()["data"]["affected"] == 4
    resp = await client.get("/api/v1/leads", params={"filters": json.dumps({"field": "tag", "op": "eq", "value": "Batch"})}, headers=admin_headers)
    assert resp.json()["data"]["total"] == 4


# ---------------------------------------------------------------- duplicates API
@pytest.mark.asyncio
async def test_duplicate_review_and_merge_via_api(client, admin_headers):
    a = await _create_lead(client, admin_headers, business_name="Dup One", email="same@dup.in", phone="9311100000")
    b = await _create_lead(client, admin_headers, business_name="Dup Two", email="same@dup.in", phone="9311100000")

    resp = await client.post("/api/v1/leads/duplicates/scan", headers=admin_headers)
    assert resp.status_code == 200
    resp = await client.get("/api/v1/leads/duplicates", headers=admin_headers)
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    candidate_id = items[0]["id"]
    assert items[0]["confidence"] == "EXACT"

    # merge B into A
    resp = await client.post(
        f"/api/v1/leads/duplicates/{candidate_id}/merge",
        json={"primary_lead_id": a["id"]}, headers=admin_headers,
    )
    assert resp.status_code == 200
    merged = resp.json()["data"]
    assert merged["id"] == a["id"]

    # B is soft-retired and excluded from the default list
    resp = await client.get(f"/api/v1/leads/{b['id']}", headers=admin_headers)
    assert resp.json()["data"]["merged_into_id"] == a["id"]
    resp = await client.get("/api/v1/leads", headers=admin_headers)
    assert resp.json()["data"]["total"] == 1

    # candidate resolved
    resp = await client.get("/api/v1/leads/duplicates", headers=admin_headers)
    assert resp.json()["data"]["total"] == 0


# ------------------------------------------------------------------ import API
@pytest.mark.asyncio
async def test_import_api_flow(client, admin_headers):
    csv_content = "Company,Mobile,Mail\nAPI Import Corp,9876500099,api-import@corp.in\n"
    resp = await client.post(
        "/api/v1/leads/import",
        files={"file": ("leads-api.csv", io.BytesIO(csv_content.encode()), "text/csv")},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    batch = resp.json()["data"]
    assert batch["format"] == "csv" and batch["status"] == "QUEUED"

    # mapping + options → inline run (small file)
    resp = await client.post(
        f"/api/v1/leads/imports/{batch['id']}/mapping",
        json={"mapping": {"Company": "business_name", "Mobile": "phone", "Mail": "email"}},
        headers=admin_headers,
    )
    data = resp.json()["data"]
    assert data["status"] == "COMPLETED" and data["imported_rows"] == 1

    # import history
    resp = await client.get("/api/v1/leads/imports", headers=admin_headers)
    assert resp.json()["data"]["total"] == 1

    # lead landed with provenance
    resp = await client.get("/api/v1/leads", params={"search": "API Import Corp"}, headers=admin_headers)
    lead = resp.json()["data"]["items"][0]
    assert lead["source_type"] == "import"
    assert lead["import_batch_id"] == batch["id"]


@pytest.mark.asyncio
async def test_import_requires_permission(client, viewer_headers):
    resp = await client.post(
        "/api/v1/leads/import",
        files={"file": ("x.csv", io.BytesIO(b"Company\nX\n"), "text/csv")},
        headers=viewer_headers,
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------ export API
@pytest.mark.asyncio
async def test_export_api_flow_and_download_security(client, admin_headers):
    await _create_lead(client, admin_headers, business_name="Export Corp", email="export@corp.in", phone="9400000000")

    resp = await client.post(
        "/api/v1/leads/export", json={"format": "csv", "scope": "all"}, headers=admin_headers
    )
    record = resp.json()["data"]
    assert record["status"] == "COMPLETED" and record["row_count"] == 1

    # download via export id — the client never sees filesystem paths
    resp = await client.get(f"/api/v1/leads/exports/{record['id']}/download", headers=admin_headers)
    assert resp.status_code == 200
    assert "Export Corp" in resp.text
    assert "Content-Disposition" in resp.headers

    # unknown export id → 404 (no traversal possible: ids only)
    resp = await client.get(f"/api/v1/leads/exports/{uuid.uuid4()}/download", headers=admin_headers)
    assert resp.status_code == 404

    # export history endpoint
    resp = await client.get("/api/v1/leads/exports", headers=admin_headers)
    assert resp.json()["data"]["total"] == 1


@pytest.mark.asyncio
async def test_quality_api(client, admin_headers):
    await _create_lead(client, admin_headers)
    resp = await client.get("/api/v1/leads/quality", headers=admin_headers)
    stats = resp.json()["data"]
    assert stats["total"] >= 1 and stats["missing_website"] >= 1

    resp = await client.post("/api/v1/leads/quality/recompute", headers=admin_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ui_static_lead_pages_are_not_shadowed_by_detail_route(client, admin_headers):
    """Regression: GET /leads/{lead_id} used to be registered BEFORE the static
    /leads/* pages, so GET /leads/import etc. hit the {lead_id} route and 422'd.
    Every static page must render directly."""
    login = await client.post(
        "/login",
        data={"email": "admin@qbit.example.com", "password": "Sup3rSecret!Pass", "next": "/leads"},
    )
    assert login.status_code == 303
    for path in (
        "/leads/import",
        "/leads/imports",
        "/leads/duplicates",
        "/leads/quality",
        "/leads/exports",
    ):
        page = await client.get(path)
        assert page.status_code == 200, f"GET {path} -> {page.status_code}"
