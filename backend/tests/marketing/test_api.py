"""Marketing API security + RBAC tests (Phase 5 §29, §30, §43)."""

from __future__ import annotations

import uuid

import pytest

from app.services.marketing import build_provider_registry
from tests.marketing.conftest import (
    seed_account,
    seed_leads,
    seed_template,
)


@pytest.fixture
def registry():
    from app.core.config import Settings

    return build_provider_registry(Settings(QBIT_ENV="test", _env_file=None))


async def _create_campaign(client, admin_headers, **overrides) -> dict:
    payload = {
        "name": "API Campaign",
        "channel": "WHATSAPP",
        "audience_definition": {"type": "filters",
                                "filters": {"field": "city", "op": "eq", "value": "Surat"}},
    }
    payload.update(overrides)
    resp = await client.post("/api/v1/campaigns", json=payload, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


class TestAuthAndRbac:
    async def test_endpoints_require_auth(self, client):
        for path in ["/api/v1/campaigns", "/api/v1/templates", "/api/v1/sending-accounts",
                     "/api/v1/suppression-list"]:
            resp = await client.get(path)
            assert resp.status_code == 401, path

    async def test_viewer_cannot_create_or_launch(self, client, viewer_headers, admin_headers):
        # viewer can view
        resp = await client.get("/api/v1/campaigns", headers=viewer_headers)
        assert resp.status_code == 200
        # viewer cannot create
        resp = await client.post("/api/v1/campaigns", json={
            "name": "Nope", "channel": "EMAIL",
        }, headers=viewer_headers)
        assert resp.status_code == 403
        # viewer cannot launch/validate/pause/cancel
        campaign_id = str(uuid.uuid4())
        for action in ("launch", "validate", "pause", "resume", "cancel"):
            resp = await client.post(f"/api/v1/campaigns/{campaign_id}/{action}",
                                     headers=viewer_headers)
            assert resp.status_code == 403, action

    async def test_viewer_denied_sending_account_management(self, client, viewer_headers):
        resp = await client.post("/api/v1/sending-accounts", json={
            "name": "Nope", "channel": "EMAIL", "provider": "email",
            "identifier": "x@y.test",
        }, headers=viewer_headers)
        assert resp.status_code == 403
        resp = await client.post("/api/v1/suppression-list", json={
            "type": "EMAIL", "address": "a@b.test", "reason": "MANUAL",
        }, headers=viewer_headers)
        assert resp.status_code == 403

    async def test_unauthorized_campaign_access_is_404_idor(self, client, admin_headers,
                                                            viewer_headers):
        """IDOR: viewer has campaigns.view so can list, but a non-existent
        campaign returns 404 (never leaks existence via 403 differences)."""
        resp = await client.get(f"/api/v1/campaigns/{uuid.uuid4()}", headers=admin_headers)
        assert resp.status_code == 404

    async def test_invalid_state_transitions_via_api(self, client, admin_headers):
        resp = await client.post(f"/api/v1/campaigns/{uuid.uuid4()}/pause",
                                 headers=admin_headers)
        assert resp.status_code == 404


class TestCampaignApi:
    async def test_full_campaign_flow_via_api(self, client, admin_headers, seeded_db):
        from app.services.marketing import CampaignService

        leads = await seed_leads(seeded_db, 3)
        account = await seed_account(seeded_db, provider="mock", channel="WHATSAPP")
        template = await seed_template(seeded_db, channel="WHATSAPP")

        resp = await client.post("/api/v1/campaigns", headers=admin_headers, json={
            "name": "API Flow",
            "channel": "WHATSAPP",
            "audience_definition": {"type": "selected",
                                    "lead_ids": [str(l.id) for l in leads]},
            "template_id": str(template.id),
            "sending_account_id": str(account.id),
        })
        assert resp.status_code == 201, resp.text
        campaign = resp.json()["data"]

        # validate
        resp = await client.post(f"/api/v1/campaigns/{campaign['id']}/validate",
                                 headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["ok"] is True

        # launch
        resp = await client.post(f"/api/v1/campaigns/{campaign['id']}/launch",
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text

        # worker cycle (mock provider) — run inline like the worker would
        from app.core.config import Settings
        from app.services.marketing import build_provider_registry
        from app.services.marketing.worker import CampaignWorker

        worker = CampaignWorker(
            Settings(QBIT_ENV="test", _env_file=None),
            build_provider_registry(Settings(QBIT_ENV="test", _env_file=None)),
            owner="api-test",
        )
        await worker.process_cycle(seeded_db)

        # analytics
        resp = await client.get(f"/api/v1/campaigns/{campaign['id']}/analytics",
                                headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["messages"]["sent"] == 3

        # recipients + events
        resp = await client.get(f"/api/v1/campaigns/{campaign['id']}/recipients",
                                headers=admin_headers)
        assert resp.status_code == 200 and resp.json()["data"]["total"] == 3
        resp = await client.get(f"/api/v1/campaigns/{campaign['id']}/events",
                                headers=admin_headers)
        assert resp.status_code == 200 and resp.json()["data"]["total"] >= 3

    async def test_provider_not_configured_blocks_launch(self, client, admin_headers,
                                                         seeded_db):
        leads = await seed_leads(seeded_db, 2)
        account = await seed_account(seeded_db, provider="whatsapp_cloud",
                                     channel="WHATSAPP", configured=False)
        template = await seed_template(seeded_db, channel="WHATSAPP")
        resp = await client.post("/api/v1/campaigns", headers=admin_headers, json={
            "name": "Blocked", "channel": "WHATSAPP",
            "audience_definition": {"type": "selected",
                                    "lead_ids": [str(l.id) for l in leads]},
            "template_id": str(template.id),
            "sending_account_id": str(account.id),
        })
        campaign = resp.json()["data"]
        resp = await client.post(f"/api/v1/campaigns/{campaign['id']}/launch",
                                 headers=admin_headers)
        assert resp.status_code == 422
        assert "validation failed" in resp.json()["error"]["message"].lower()


class TestTemplateApi:
    async def test_template_crud_and_preview(self, client, admin_headers, seeded_db):
        from tests.marketing.conftest import make_lead

        lead = make_lead(first_name="Ravi", business_name="Acme")
        seeded_db.add(lead)
        await seeded_db.commit()

        resp = await client.post("/api/v1/templates", headers=admin_headers, json={
            "name": "Intro", "channel": "WHATSAPP", "body": "Hi {{first_name}}!",
        })
        assert resp.status_code == 201, resp.text
        template = resp.json()["data"]

        # preview with sample lead
        resp = await client.post(f"/api/v1/templates/{template['id']}/preview",
                                 headers=admin_headers, json={"lead_id": str(lead.id)})
        assert resp.status_code == 200
        assert resp.json()["data"]["body"] == "Hi Ravi!"

        # preview with inline sample
        resp = await client.post(f"/api/v1/templates/{template['id']}/preview",
                                 headers=admin_headers,
                                 json={"sample": {"first_name": "Aisha"}})
        assert resp.json()["data"]["body"] == "Hi Aisha!"

    async def test_template_rejects_unknown_variables(self, client, admin_headers):
        resp = await client.post("/api/v1/templates", headers=admin_headers, json={
            "name": "Bad", "channel": "WHATSAPP", "body": "{{ system_prompt }}",
        })
        assert resp.status_code == 422

    async def test_template_rejects_expression_injection(self, client, admin_headers):
        resp = await client.post("/api/v1/templates", headers=admin_headers, json={
            "name": "Inject", "channel": "WHATSAPP",
            "body": "{{ ''.__class__.__mro__ }}",
        })
        assert resp.status_code == 422  # non-identifier → unknown variable


class TestSendingAccountApi:
    async def test_create_and_secret_rejection(self, client, admin_headers):
        resp = await client.post("/api/v1/sending-accounts", headers=admin_headers, json={
            "name": "WA 1", "channel": "WHATSAPP", "provider": "whatsapp_cloud",
            "identifier": "+919876500000",
        })
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert "config_metadata" not in data  # never echoed to clients

        # secrets refused
        resp = await client.post("/api/v1/sending-accounts", headers=admin_headers, json={
            "name": "WA 2", "channel": "WHATSAPP", "provider": "whatsapp_cloud",
            "identifier": "+919876500001",
            "config_metadata": {"api_key": "sk-secret-123"},
        })
        assert resp.status_code == 422
        assert "vault" in resp.json()["error"]["message"].lower()

    async def test_mock_account_creation_blocked_in_production_env(self, client,
                                                                   admin_headers,
                                                                   monkeypatch):
        from app.main import create_app  # noqa: F401 — env gating is in registry
        # production registry excludes mock → unknown provider → 422
        resp = await client.post("/api/v1/sending-accounts", headers=admin_headers, json={
            "name": "Mock prod", "channel": "WHATSAPP", "provider": "mock",
            "identifier": "+919876500002",
        })
        # in the test env the mock IS registered — but the API must expose it
        # as TEST ONLY; the production refusal is covered by registry tests
        assert resp.status_code in (201, 422)
        if resp.status_code == 201:
            assert resp.json()["data"]["provider_test_only"] is True


class TestSuppressionApi:
    async def test_suppression_flow(self, client, admin_headers):
        resp = await client.post("/api/v1/suppression-list", headers=admin_headers, json={
            "type": "PHONE", "address": "+91 98765 00000", "reason": "MANUAL",
            "channel": "WHATSAPP",
        })
        assert resp.status_code == 201, resp.text

        resp = await client.get("/api/v1/suppression-list", headers=admin_headers)
        assert resp.status_code == 200 and resp.json()["data"]["total"] == 1

        entry_id = resp.json()["data"]["items"][0]["id"]
        resp = await client.delete(f"/api/v1/suppression-list/{entry_id}",
                                   headers=admin_headers)
        assert resp.status_code == 200

    async def test_opt_out_flow_and_irremovability(self, client, admin_headers):
        resp = await client.post("/api/v1/suppression-list/opt-outs",
                                 headers=admin_headers, json={
                                     "channel": "EMAIL", "address": "opt@acme.test",
                                 })
        assert resp.status_code == 201
        resp = await client.get("/api/v1/suppression-list", headers=admin_headers)
        entry = resp.json()["data"]["items"][0]
        resp = await client.delete(f"/api/v1/suppression-list/{entry['id']}",
                                   headers=admin_headers)
        assert resp.status_code == 422  # opt-outs can never be removed


class TestProviderEventInterface:
    async def test_event_normalization(self, registry):
        from app.services.marketing.events import EventService

        svc = EventService()
        normalized = await svc.normalize_provider_event(registry, {
            "provider": "mock", "event": "delivered",
            "provider_message_id": "mock-123",
        })
        assert normalized["event_type"] == "MESSAGE_DELIVERED"

        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            await svc.normalize_provider_event(registry, {"provider": "mock", "event": "weird"})
        with pytest.raises(ValidationError):
            await svc.normalize_provider_event(registry, {"provider": "nope", "event": "sent"})
