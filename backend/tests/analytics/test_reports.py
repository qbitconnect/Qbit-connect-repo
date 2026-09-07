"""Report lifecycle tests (spec §17–§19): create, edit, duplicate, archive,
restore, background run execution, snapshot export."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


LEAD_CONFIG = {
    "domain": "LEADS",
    "metrics": ["total", "converted"],
    "dimensions": ["source"],
    "period": "30d",
    "visualization": "table",
    "limit": 50,
}


async def _create(client, headers, config=None, visibility="PRIVATE", name="R"):
    effective = config or LEAD_CONFIG
    resp = await client.post("/api/v1/reports", json={
        "name": name, "domain": effective.get("domain", "LEADS"),
        "config": effective, "visibility": visibility,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


class TestReportCrud:
    async def test_create_edit_duplicate_archive_restore(self, client, admin_auth):
        report = await _create(client, admin_auth, name="Weekly leads")
        assert report["status"] == "ACTIVE"
        assert report["config"]["metrics"] == ["total", "converted"]

        # edit bumps the config version
        resp = await client.put(f"/api/v1/reports/{report['id']}", json={
            "config": {**LEAD_CONFIG, "metrics": ["total"]},
        }, headers=admin_auth)
        assert resp.status_code == 200
        assert resp.json()["data"]["config_version"] == 2

        # duplicate starts private with the same config
        resp = await client.post(f"/api/v1/reports/{report['id']}/duplicate",
                                 headers=admin_auth)
        assert resp.status_code == 201
        copy = resp.json()["data"]
        assert copy["name"] == "Weekly leads (copy)"
        assert copy["visibility"] == "PRIVATE"
        assert copy["id"] != report["id"]

        # archive → restore
        resp = await client.post(f"/api/v1/reports/{report['id']}/archive",
                                 headers=admin_auth)
        assert resp.json()["data"]["status"] == "ARCHIVED"
        resp = await client.post(f"/api/v1/reports/{report['id']}/restore",
                                 headers=admin_auth)
        assert resp.json()["data"]["status"] == "ACTIVE"

        # delete
        resp = await client.delete(f"/api/v1/reports/{report['id']}",
                                   headers=admin_auth)
        assert resp.status_code == 200
        resp = await client.get(f"/api/v1/reports/{report['id']}", headers=admin_auth)
        assert resp.status_code == 404

    async def test_archived_reports_hidden_from_list(self, client, admin_auth):
        report = await _create(client, admin_auth, name="Archived me")
        await client.post(f"/api/v1/reports/{report['id']}/archive", headers=admin_auth)
        resp = await client.get("/api/v1/reports", headers=admin_auth)
        ids = [r["id"] for r in resp.json()["data"]["items"]]
        assert report["id"] not in ids


class TestReportRuns:
    async def test_run_lifecycle_produces_snapshot_and_export(self, client, admin_auth,
                                                              app, analytics_data):
        report = await _create(client, admin_auth, name="Run me")
        # queue the run
        resp = await client.post(f"/api/v1/reports/{report['id']}/run",
                                 json={"format": "json"}, headers=admin_auth)
        assert resp.status_code == 202
        run = resp.json()["data"]
        assert run["status"] == "QUEUED"

        # execute it the way the worker loop does
        from app.analytics.reports.executor import ReportWorker
        from app.services.audit import AuditService
        from app.services.files import FileService
        from app.services.storage import StorageService

        worker = ReportWorker(
            owner="test-worker",
            storage_files=FileService(StorageService(app.state.settings), AuditService()),
        )
        db = app.state.db
        async with db.session() as session:
            processed = await worker.process_cycle(session)
        assert processed == 1

        # run is completed with a real snapshot
        resp = await client.get(f"/api/v1/reports/{report['id']}/runs/{run['id']}",
                                headers=admin_auth)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "COMPLETED"
        snapshot = data["snapshot"]
        assert snapshot is not None
        rows = snapshot["data"]["rows"]
        assert snapshot["row_count"] == len(rows) > 0
        # real values from the fixture: source rows with leads >= 1
        total_leads = sum(r.get("leads", 0) for r in rows if "leads" in r)
        assert total_leads == 8

        # export the snapshot
        resp = await client.get(f"/api/v1/reports/{report['id']}/export?format=csv",
                                headers=admin_auth)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert b"source" in resp.content or b"value" in resp.content

    async def test_run_records_failure_for_impossible_series(self, client, admin_auth,
                                                             app):
        """A MARKETING line report has no day series for email-only dims — it
        must fail HONESTLY, not fabricate (spec §35)."""
        config = {"domain": "EMAIL", "metrics": ["delivered"],
                  "visualization": "line", "period": "30d"}
        report = await _create(client, admin_auth, config=config, name="Bad series")
        resp = await client.post(f"/api/v1/reports/{report['id']}/run",
                                 json={"format": "json"}, headers=admin_auth)
        run = resp.json()["data"]

        from app.analytics.reports.executor import ReportWorker
        from app.services.audit import AuditService
        from app.services.files import FileService
        from app.services.storage import StorageService

        worker = ReportWorker(
            owner="test-worker",
            storage_files=FileService(StorageService(app.state.settings), AuditService()),
        )
        db = app.state.db
        async with db.session() as session:
            await worker.process_cycle(session)
        resp = await client.get(f"/api/v1/reports/{report['id']}/runs/{run['id']}",
                                headers=admin_auth)
        data = resp.json()["data"]
        assert data["status"] == "FAILED"
        assert data["error"]  # honest error message persisted

    async def test_reproducible_run_uses_frozen_config(self, client, admin_auth,
                                                       app, analytics_data):
        """Editing the report after queueing must not change the running
        config (spec §19 reproducibility)."""
        report = await _create(client, admin_auth, name="Frozen")
        resp = await client.post(f"/api/v1/reports/{report['id']}/run",
                                 json={"format": "json"}, headers=admin_auth)
        run = resp.json()["data"]
        # change the report while the run is queued
        await client.put(f"/api/v1/reports/{report['id']}", json={
            "config": {**LEAD_CONFIG, "metrics": ["total"]},
        }, headers=admin_auth)
        db = app.state.db
        from app.models.analytics import ReportRun

        async with db.session() as session:
            stored = await session.get(ReportRun, uuid.UUID(run["id"]))
        assert stored.config_snapshot["metrics"] == ["total", "converted"]
        assert stored.config_version == 1  # the version at queue time


class TestReportExportHistory:
    async def test_export_without_run_404s(self, client, admin_auth):
        report = await _create(client, admin_auth, name="No runs yet")
        resp = await client.get(f"/api/v1/reports/{report['id']}/export?format=csv",
                                headers=admin_auth)
        assert resp.status_code == 404

    async def test_run_for_archived_report_rejected(self, client, admin_auth, app):
        report = await _create(client, admin_auth, name="Archived")
        await client.post(f"/api/v1/reports/{report['id']}/archive", headers=admin_auth)
        resp = await client.post(f"/api/v1/reports/{report['id']}/run",
                                 json={"format": "json"}, headers=admin_auth)
        assert resp.status_code == 400
