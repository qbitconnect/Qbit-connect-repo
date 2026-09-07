"""Automation API + RBAC + audit tests (§61, §62, §69) + TEST 9."""

import uuid

import pytest

from tests.automation.conftest import seed_workflow


GOOD_DEFINITION = {
    "nodes": [
        {"id": "trigger", "type": "TRIGGER",
         "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "tag"},
        {"id": "tag", "type": "ACTION", "action": "add_tag",
         "config": {"tag": "Auto"}, "next_node_id": "end"},
        {"id": "end", "type": "END"},
    ],
}


async def _create(client, admin_headers, **overrides):
    payload = {
        "name": overrides.get("name", "API WF"),
        "description": "created via API",
        "trigger_type": "LEAD_CREATED",
        "definition": overrides.get("definition", GOOD_DEFINITION),
    }
    resp = await client.post("/api/v1/automation/workflows", json=payload,
                             headers=admin_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


class TestWorkflowAPI:
    async def test_create_list_get(self, client, admin_headers):
        wf = await _create(client, admin_headers, name="List WF")
        assert wf["status"] == "DRAFT"

        resp = await client.get("/api/v1/automation/workflows", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] >= 1
        assert any(i["name"] == "List WF" for i in data["items"])

        detail = await client.get(f"/api/v1/automation/workflows/{wf['id']}",
                                  headers=admin_headers)
        assert detail.status_code == 200
        body = detail.json()["data"]
        assert body["definition"]["nodes"][0]["type"] == "TRIGGER"
        assert body["stats"]["executions_total"] == 0

    async def test_validate_publish_pause_resume_archive(self, client, admin_headers):
        wf = await _create(client, admin_headers, name="Lifecycle WF")

        v = await client.post(f"/api/v1/automation/workflows/{wf['id']}/validate",
                              headers=admin_headers)
        assert v.status_code == 200 and v.json()["data"]["ok"] is True

        p = await client.post(f"/api/v1/automation/workflows/{wf['id']}/publish",
                              headers=admin_headers)
        assert p.status_code == 200
        assert p.json()["data"]["workflow"]["status"] == "ACTIVE"
        assert p.json()["data"]["version"]["status"] == "PUBLISHED"

        pa = await client.post(f"/api/v1/automation/workflows/{wf['id']}/pause",
                               headers=admin_headers)
        assert pa.json()["data"]["status"] == "PAUSED"
        re_ = await client.post(f"/api/v1/automation/workflows/{wf['id']}/resume",
                                headers=admin_headers)
        assert re_.json()["data"]["status"] == "ACTIVE"
        ar = await client.post(f"/api/v1/automation/workflows/{wf['id']}/archive",
                               headers=admin_headers)
        assert ar.json()["data"]["status"] == "ARCHIVED"

    async def test_invalid_workflow_never_created(self, client, admin_headers):
        """§21/§46: invalid definitions are rejected — they can never be
        created, published or activated."""
        bad = {"nodes": [
            {"id": "trigger", "type": "TRIGGER",
             "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "end"},
            {"id": "end", "type": "END", "next_node_id": "trigger"},  # END with next
        ]}
        resp = await client.post("/api/v1/automation/workflows", json={
            "name": "Bad WF", "trigger_type": "LEAD_CREATED", "definition": bad,
        }, headers=admin_headers)
        assert resp.status_code in (400, 422)  # rejected — never created

    async def test_duplicate_creates_draft(self, client, admin_headers):
        wf = await _create(client, admin_headers, name="Dup WF")
        d = await client.post(f"/api/v1/automation/workflows/{wf['id']}/duplicate",
                              headers=admin_headers)
        assert d.status_code == 201
        copy = d.json()["data"]
        assert copy["status"] == "DRAFT"
        assert copy["name"] == "Dup WF (copy)"

    async def test_versions_listed(self, client, admin_headers):
        wf = await _create(client, admin_headers, name="Ver WF")
        await client.post(f"/api/v1/automation/workflows/{wf['id']}/publish",
                          headers=admin_headers)
        resp = await client.get(f"/api/v1/automation/workflows/{wf['id']}/versions",
                                headers=admin_headers)
        versions = resp.json()["data"]["items"]
        assert [v["version"] for v in versions] == [1]

    async def test_templates_endpoint(self, client, admin_headers):
        resp = await client.get("/api/v1/automation/templates", headers=admin_headers)
        items = resp.json()["data"]["items"]
        assert len(items) == 5
        ft = await client.post("/api/v1/automation/workflows/from-template",
                               json={"template_id": "new_lead_qualification"},
                               headers=admin_headers)
        assert ft.status_code == 201
        assert ft.json()["data"]["status"] == "DRAFT"  # never auto-activated (§52)

    async def test_create_with_unknown_trigger_rejected(self, client, admin_headers):
        resp = await client.post("/api/v1/automation/workflows", json={
            "name": "Bad", "trigger_type": "NOT_A_TRIGGER",
            "definition": GOOD_DEFINITION,
        }, headers=admin_headers)
        assert resp.status_code in (400, 422)

    async def test_unknown_workflow_404(self, client, admin_headers):
        resp = await client.get(f"/api/v1/automation/workflows/{uuid.uuid4()}",
                                headers=admin_headers)
        assert resp.status_code == 404


class TestExecutionAPI:
    async def test_executions_list_and_detail(self, client, admin_headers, seeded_db):
        from tests.automation.conftest import make_lead

        lead = make_lead()
        seeded_db.add(lead)
        await seed_workflow(seeded_db)
        from app.automation.services.event_dispatcher import dispatch_event

        await dispatch_event(seeded_db, event_type="lead.created", entity_type="lead",
                             entity_id=lead.id, payload={}, event_id=f"e:{uuid.uuid4()}")
        await seeded_db.commit()

        resp = await client.get("/api/v1/automation/executions", headers=admin_headers)
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) >= 1

        detail = await client.get(
            f"/api/v1/automation/executions/{items[0]['id']}", headers=admin_headers)
        assert detail.status_code == 200
        assert "steps" in detail.json()["data"]

    async def test_cancel_execution(self, client, admin_headers, seeded_db):
        from tests.automation.conftest import make_lead

        lead = make_lead()
        seeded_db.add(lead)
        await seed_workflow(seeded_db)
        from app.automation.services.event_dispatcher import dispatch_event

        created = await dispatch_event(seeded_db, event_type="lead.created",
                                       entity_type="lead", entity_id=lead.id,
                                       payload={}, event_id=f"e:{uuid.uuid4()}")
        await seeded_db.commit()

        resp = await client.post(
            f"/api/v1/automation/executions/{created[0].id}/cancel",
            headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "CANCELLED"


class TestRBAC:
    """TEST 9 + §61: only authorized users may mutate/publish."""

    async def test_viewer_read_only(self, client, admin_headers, viewer_headers):
        wf = await _create(client, admin_headers, name="RBAC WF")

        # viewer can read
        assert (await client.get("/api/v1/automation/workflows",
                                 headers=viewer_headers)).status_code == 200
        assert (await client.get(
            f"/api/v1/automation/workflows/{wf['id']}",
            headers=viewer_headers)).status_code == 200

        # viewer cannot create/edit/publish/pause/resume/archive/delete
        assert (await client.post("/api/v1/automation/workflows", json={
            "name": "Nope", "trigger_type": "LEAD_CREATED",
            "definition": GOOD_DEFINITION}, headers=viewer_headers)
        ).status_code == 403
        assert (await client.patch(f"/api/v1/automation/workflows/{wf['id']}",
                                   json={"name": "Nope"},
                                   headers=viewer_headers)).status_code == 403
        assert (await client.post(
            f"/api/v1/automation/workflows/{wf['id']}/publish",
            headers=viewer_headers)).status_code == 403
        assert (await client.post(
            f"/api/v1/automation/workflows/{wf['id']}/pause",
            headers=viewer_headers)).status_code == 403
        assert (await client.post(
            f"/api/v1/automation/workflows/{wf['id']}/resume",
            headers=viewer_headers)).status_code == 403
        assert (await client.post(
            f"/api/v1/automation/workflows/{wf['id']}/archive",
            headers=viewer_headers)).status_code == 403
        assert (await client.delete(f"/api/v1/automation/workflows/{wf['id']}",
                                    headers=viewer_headers)).status_code == 403
        # executions viewable but cancel forbidden
        assert (await client.get("/api/v1/automation/executions",
                                 headers=viewer_headers)).status_code == 200
        assert (await client.post(
            f"/api/v1/automation/executions/{uuid.uuid4()}/cancel",
            headers=viewer_headers)).status_code == 403

    async def test_unauthenticated_401(self, client):
        assert (await client.get("/api/v1/automation/workflows")).status_code == 401
        assert (await client.post("/api/v1/automation/workflows", json={})).status_code == 401


class TestAudit:
    async def test_workflow_lifecycle_audited(self, client, admin_headers, seeded_db):
        wf = await _create(client, admin_headers, name="Audit WF")
        await client.post(f"/api/v1/automation/workflows/{wf['id']}/publish",
                          headers=admin_headers)
        await client.post(f"/api/v1/automation/workflows/{wf['id']}/pause",
                          headers=admin_headers)
        from sqlalchemy import select

        from app.models.audit import AuditLog

        rows = (await seeded_db.execute(
            select(AuditLog).where(AuditLog.resource_id == wf["id"])
            .order_by(AuditLog.created_at))).scalars().all()
        actions = {r.action for r in rows}
        assert "automation.workflow_created" in actions
        assert "automation.workflow_published" in actions
        assert "automation.workflow_paused" in actions
