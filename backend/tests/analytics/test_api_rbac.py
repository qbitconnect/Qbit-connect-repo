"""Analytics API + RBAC + security tests (spec §24, §26, §32)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


class TestPermissionMatrix:
    @pytest.mark.parametrize("path,permission", [
        ("/api/v1/analytics/overview", "analytics.view"),
        ("/api/v1/analytics/leads", "analytics.view_leads"),
        ("/api/v1/analytics/leads/sources", "analytics.view_leads"),
        ("/api/v1/analytics/leads/funnel", "analytics.view_leads"),
        ("/api/v1/analytics/scraping", "analytics.view_scraping"),
        ("/api/v1/analytics/marketing", "analytics.view_marketing"),
        ("/api/v1/analytics/whatsapp", "analytics.view_whatsapp"),
        ("/api/v1/analytics/email", "analytics.view_email"),
        ("/api/v1/analytics/inbox", "analytics.view_inbox"),
        ("/api/v1/analytics/automation", "analytics.view_automation"),
    ])
    async def test_viewer_can_read_domains(self, client, viewer_auth, path, permission):
        resp = await client.get(path, headers=viewer_auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True

    async def test_team_denied_to_viewer(self, client, viewer_auth):
        resp = await client.get("/api/v1/analytics/team", headers=viewer_auth)
        assert resp.status_code == 403

    async def test_diagnostics_denied_to_viewer(self, client, viewer_auth):
        resp = await client.get("/api/v1/analytics/diagnostics", headers=viewer_auth)
        assert resp.status_code == 403

    async def test_rebuild_denied_to_viewer(self, client, viewer_auth):
        resp = await client.post("/api/v1/analytics/aggregates/rebuild",
                                 headers=viewer_auth, json={})
        assert resp.status_code == 403

    async def test_diagnostics_allowed_for_admin(self, client, admin_auth):
        resp = await client.get("/api/v1/analytics/diagnostics", headers=admin_auth)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["checks_total"] >= 6
        assert "anomalies" in data

    async def test_analytics_requires_auth(self, client):
        resp = await client.get("/api/v1/analytics/overview")
        assert resp.status_code in (401, 403)


class TestQuerySecurity:
    async def test_unknown_filter_rejected(self, client, admin_auth):
        resp = await client.get("/api/v1/analytics/leads",
                                params={"raw_sql": "1=1; DROP TABLE leads"},
                                headers=admin_auth)
        assert resp.status_code == 400
        assert "Unknown filter" in resp.json()["error"]["message"]

    async def test_unknown_period_rejected(self, client, admin_auth):
        resp = await client.get("/api/v1/analytics/overview",
                                params={"period": "ninety centuries"},
                                headers=admin_auth)
        assert resp.status_code == 400

    async def test_injection_value_returns_clean_empty(self, client, admin_auth):
        resp = await client.get("/api/v1/analytics/leads",
                                params={"city": "x' OR '1'='1--"},
                                headers=admin_auth)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kpis"]["total"] == 0  # hostile value matches nothing

    async def test_invalid_timezone_rejected(self, client, admin_auth):
        resp = await client.get("/api/v1/analytics/overview",
                                params={"timezone": "Not/AZone"},
                                headers=admin_auth)
        assert resp.status_code == 400

    async def test_no_secrets_in_responses(self, client, admin_auth, analytics_data):
        for path in ("/api/v1/analytics/overview", "/api/v1/analytics/whatsapp",
                     "/api/v1/analytics/email", "/api/v1/analytics/marketing"):
            resp = await client.get(path, headers=admin_auth)
            assert resp.status_code == 200
            text = resp.text.lower()
            for secret_marker in ("access_token", "ciphertext", "password",
                                  "smtp_password", "secret_key"):
                assert secret_marker not in text, f"{path} leaked {secret_marker}"


class TestCacheIsolation:
    async def test_team_scope_differs_between_users(self, app, analytics_data):
        """Different scope strings must produce different cache keys — one
        user's restricted analytics can never be served to another (spec §22)."""
        from app.analytics.core.cache import AnalyticsCache

        cache = AnalyticsCache(None)
        payload = {"filters": {"city": ["Pune"]}}
        key_a = cache.build_key("team", payload, scope="user-a")
        key_b = cache.build_key("team", payload, scope="user-b")
        assert key_a != key_b

    async def test_filter_changes_change_key(self, app, analytics_data):
        from app.analytics.core.cache import AnalyticsCache

        cache = AnalyticsCache(None)
        base = {"filters": {}}
        assert cache.build_key("leads", base, "global") != cache.build_key(
            "leads", {"filters": {"source": ["import"]}}, "global")


class TestReportsAuthorization:
    async def _create_report(self, client, headers, **overrides):
        payload = {
            "name": "Leads weekly",
            "description": "Test report",
            "domain": "LEADS",
            "config": {"domain": "LEADS", "metrics": ["total", "converted"],
                       "dimensions": ["source"], "period": "30d",
                       "visualization": "table"},
            "visibility": "PRIVATE",
        }
        payload.update(overrides)
        resp = await client.post("/api/v1/reports", json=payload, headers=headers)
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]

    async def test_owner_sees_private_report(self, client, admin_auth):
        report = await self._create_report(client, admin_auth)
        resp = await client.get(f"/api/v1/reports/{report['id']}", headers=admin_auth)
        assert resp.status_code == 200

    async def test_private_report_hidden_from_other_users(self, client, admin_auth,
                                                          viewer_auth):
        """IDOR posture: foreign PRIVATE reports 404, never leak (spec §32)."""
        report = await self._create_report(client, admin_auth)
        resp = await client.get(f"/api/v1/reports/{report['id']}", headers=viewer_auth)
        assert resp.status_code == 404

    async def test_viewer_cannot_create_reports(self, client, viewer_auth):
        resp = await client.post("/api/v1/reports", json={
            "name": "nope", "domain": "LEADS",
            "config": {"domain": "LEADS", "metrics": ["total"]},
        }, headers=viewer_auth)
        assert resp.status_code == 403

    async def test_non_owner_cannot_edit(self, client, admin_auth, viewer_auth):
        report = await self._create_report(client, admin_auth, visibility="GLOBAL")
        resp = await client.put(f"/api/v1/reports/{report['id']}",
                                json={"name": "hijacked"}, headers=viewer_auth)
        assert resp.status_code == 403

    async def test_team_visibility_visible_but_not_editable(self, client, admin_auth,
                                                            viewer_auth):
        report = await self._create_report(client, admin_auth, visibility="TEAM")
        resp = await client.get(f"/api/v1/reports/{report['id']}", headers=viewer_auth)
        assert resp.status_code == 200
        resp = await client.put(f"/api/v1/reports/{report['id']}",
                                json={"name": "renamed"}, headers=viewer_auth)
        assert resp.status_code == 403

    async def test_invalid_config_rejected_with_422(self, client, admin_auth):
        resp = await client.post("/api/v1/reports", json={
            "name": "Bad", "domain": "LEADS",
            "config": {"domain": "LEADS", "metrics": ["made_up_metric"]},
        }, headers=admin_auth)
        assert resp.status_code == 422

    async def test_missing_report_404(self, client, admin_auth):
        resp = await client.get(f"/api/v1/reports/{uuid.uuid4()}", headers=admin_auth)
        assert resp.status_code == 404
