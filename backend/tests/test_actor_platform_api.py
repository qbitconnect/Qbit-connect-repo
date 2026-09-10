"""Actor Platform API tests (spec §23/§40) — actors, runs, datasets, tasks,
run webhooks, storage. Acceptance TEST 10 (task re-run) is covered here.

Runs are exercised end-to-end through the API + a real worker runner pass
(in-process queue), so dataset capture and run outcomes are verified against
the REAL pipeline, not stubs.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.scrapers.test_new_actors_runs import AUTO_HTML, IM_SEARCH_HTML

pytestmark = pytest.mark.asyncio


async def _wait_terminal(app, client, headers, run_id: str, *, timeout_s: float = 25.0) -> dict:
    """Drive the worker loop until the run reaches a terminal state."""
    import uuid as _uuid

    from app.models.scrape import JobStatus, ScrapeJob
    from app.services.scraping.runner import JobRunner

    runner = JobRunner(
        settings=app.state.settings,
        session_factory=app.state.db.session,
        storage_root=app.state.settings.QBIT_DATA_DIR / "scraper-results",
        queue=app.state.queue,
        owner="test-worker",
    )
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
        body = resp.json()["data"]
        if body["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return body
        job_id = _uuid.UUID(run_id)
        actor_id = body["actor_id"]
        if body["status"] in ("QUEUED", "PAUSED"):
            actor = app.state.scraper_registry.get(actor_id)
            await runner.execute(job_id, actor)
            continue
        await asyncio.sleep(0.1)
    raise TimeoutError(f"run {run_id} did not terminate in time")


# ------------------------------------------------------------------ actors
async def test_actors_list_with_stats_and_health(client, admin_headers):
    resp = await client.get("/api/v1/actors", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    slugs = {a["id"] for a in body["data"]}
    assert {"instagram", "meta-ads-library", "linkedin-public", "justdial", "indiamart"} <= slugs
    for actor in body["data"]:
        assert "stats" in actor and "input_schema" in actor
    insta = next(a for a in body["data"] if a["id"] == "instagram")
    assert insta["category"] == "social_media"


async def test_actor_detail_includes_schema_and_examples(client, admin_headers):
    resp = await client.get("/api/v1/actors/justdial", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["input_schema"]["properties"]["city"]
    assert "stats" in data and "health_history" in data


async def test_actor_404(client, admin_headers):
    resp = await client.get("/api/v1/actors/nope-actor", headers=admin_headers)
    assert resp.status_code == 404


async def test_actors_list_requires_auth(client):
    resp = await client.get("/api/v1/actors")
    assert resp.status_code == 401


# ------------------------------------------------------------------ runs + datasets
async def _make_job_via_api(client, headers, actor_id, input_data, **kwargs):
    resp = await client.post(
        f"/api/v1/actors/{actor_id}/runs",
        headers=headers,
        json={"input": input_data, **kwargs},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def test_run_to_dataset_local_http(app, client, admin_headers):
    """Full chain: POST /actors/{slug}/runs → worker → dataset → export."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    page = AUTO_HTML.replace("https://fixture.test/canonical", "http://127.0.0.1/canonical")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # allow loopback targets for this run (private-target guard is
        # config-driven, spec §6 LEVEL 1 needs it for the local fixture server)
        app.state.settings.QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS = True
        job = await _make_job_via_api(
            client, admin_headers, "universal-web",
            {"url": f"http://127.0.0.1:{port}/", "strategy": "auto"},
        )
        terminal = await _wait_terminal(app, client, admin_headers, job["id"])
        assert terminal["status"] == "COMPLETED", terminal
        assert terminal["outcome"] == "SUCCEEDED"
        assert terminal["dataset_id"]

        ds = await client.get(f"/api/v1/datasets/{terminal['dataset_id']}", headers=admin_headers)
        assert ds.status_code == 200
        dataset = ds.json()["data"]
        assert dataset["item_count"] >= 1
        assert dataset["status"] == "READY"

        items = await client.get(
            f"/api/v1/datasets/{dataset['id']}/items", headers=admin_headers
        )
        assert items.status_code == 200
        first = items.json()["data"][0]["data"]
        assert first["metadata"]["extraction_strategy"] == "auto"

        # search filter
        filtered = await client.get(
            f"/api/v1/datasets/{dataset['id']}/items",
            headers=admin_headers, params={"q": "Fixture"},
        )
        assert filtered.status_code == 200
        assert filtered.json()["meta"]["total"] >= 1

        # exports (acceptance TEST 1 tail: CSV + JSON)
        for fmt, expected_type in (("csv", "text/csv"), ("json", "application/json"), ("jsonl", "application/x-ndjson")):
            export = await client.post(
                f"/api/v1/datasets/{dataset['id']}/export",
                headers=admin_headers, json={"format": fmt},
            )
            assert export.status_code == 200, export.text
            assert export.headers["content-type"].startswith(expected_type)
            assert len(export.content) > 10
        xlsx = await client.post(
            f"/api/v1/datasets/{dataset['id']}/export",
            headers=admin_headers, json={"format": "xlsx"},
        )
        assert xlsx.status_code == 200
        assert xlsx.content[:2] == b"PK"  # zip magic

        # run logs endpoint
        logs = await client.get(f"/api/v1/runs/{job['id']}/logs", headers=admin_headers)
        assert logs.status_code == 200
        kinds = {e["event_type"] for e in logs.json()["data"]}
        assert "JOB_COMPLETED" in kinds
    finally:
        app.state.settings.QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS = False
        server.shutdown()


async def test_runs_list_and_filters(client, admin_headers):
    resp = await client.get(
        "/api/v1/runs", headers=admin_headers, params={"limit": 5}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body and "meta" in body


# ------------------------------------------------------------------ tasks
async def test_task_lifecycle_and_rerun(client, admin_headers):
    """Acceptance TEST 10: saved Task re-runs with its saved configuration."""
    create = await client.post(
        "/api/v1/tasks", headers=admin_headers,
        json={
            "actor_id": "indiamart",
            "name": "Leather shoes Delhi",
            "input": {"mode": "product_search", "keyword": "leather shoes"},
            "config": {"max_records": 10},
        },
    )
    assert create.status_code == 200, create.text
    task = create.json()["data"]

    # duplicate
    dup = await client.post(f"/api/v1/tasks/{task['id']}/duplicate", headers=admin_headers)
    assert dup.status_code == 200
    assert dup.json()["data"]["name"] == "Leather shoes Delhi (copy)"

    # update
    upd = await client.patch(
        f"/api/v1/tasks/{task['id']}", headers=admin_headers,
        json={"name": "Leather shoes Delhi v2"},
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["name"] == "Leather shoes Delhi v2"

    # list + get
    listing = await client.get("/api/v1/tasks", headers=admin_headers)
    assert listing.status_code == 200
    assert listing.json()["meta"]["total"] >= 2

    # run the task (job created, queued — worker not required for the API path)
    run = await client.post(f"/api/v1/tasks/{task['id']}/run", headers=admin_headers)
    assert run.status_code == 200, run.text
    run_body = run.json()["data"]
    assert run_body["trigger"] == "TASK"
    assert run_body["name"] == "Leather shoes Delhi v2"
    assert run_body["task_id"] == task["id"]

    # re-run reuses the SAME saved input (TEST 10)
    run2 = await client.post(f"/api/v1/tasks/{task['id']}/run", headers=admin_headers)
    assert run2.status_code == 200
    assert run2.json()["data"]["input"] == run_body["input"]

    detail = await client.get(f"/api/v1/tasks/{task['id']}", headers=admin_headers)
    assert detail.json()["data"]["run_count"] == 2

    # delete
    delete = await client.delete(f"/api/v1/tasks/{task['id']}", headers=admin_headers)
    assert delete.status_code == 200
    gone = await client.get(f"/api/v1/tasks/{task['id']}", headers=admin_headers)
    assert gone.status_code == 404


async def test_task_requires_valid_actor_input(client, admin_headers):
    bad = await client.post(
        "/api/v1/tasks", headers=admin_headers,
        json={"actor_id": "indiamart", "name": "bad", "input": {"mode": "product_search"}},
    )
    assert bad.status_code == 422  # missing keyword for search mode


# ------------------------------------------------------------------ run webhooks
async def test_run_webhook_crud_and_delivery(app, client, admin_headers):
    from app.services.scraping.run_webhooks import deliver_pending_webhooks, sign_payload

    created = await client.post(
        "/api/v1/run-webhooks", headers=admin_headers,
        json={
            "name": "Run completions",
            "url": "http://127.0.0.1:9/webhook",  # unreachable — retries honestly
            "events": ["RUN_SUCCEEDED"],
            "actor_id": "universal-web",
        },
    )
    assert created.status_code == 200, created.text
    hook = created.json()["data"]
    assert hook["has_secret"] is True
    assert "secret" in hook  # generated secret revealed ONCE at creation
    secret = hook["secret"]

    listing = await client.get("/api/v1/run-webhooks", headers=admin_headers)
    assert listing.status_code == 200
    assert listing.json()["meta"]["total"] == 1
    # secret never leaks again
    assert "secret" not in listing.json()["data"][0]

    # test event → queued delivery
    test_fire = await client.post(f"/api/v1/run-webhooks/{hook['id']}/test", headers=admin_headers)
    assert test_fire.status_code == 200

    deliveries = await client.get(
        f"/api/v1/run-webhooks/{hook['id']}/deliveries", headers=admin_headers
    )
    assert deliveries.status_code == 200
    assert deliveries.json()["meta"]["count"] == 1

    # worker delivery tick: unreachable endpoint → attempt recorded honestly
    delivered = await deliver_pending_webhooks(app.state.db.session_factory)
    assert delivered == 0
    after = await client.get(
        f"/api/v1/run-webhooks/{hook['id']}/deliveries", headers=admin_headers
    )
    row = after.json()["data"][0]
    assert row["attempts"] == 1
    assert row["status"] in ("PENDING", "FAILED")  # retry scheduled or exhausted

    # signature helper sanity (contract used by receivers)
    sig = sign_payload(secret, "123", b"{}")
    assert len(sig) == 64

    # disable + update
    upd = await client.patch(
        f"/api/v1/run-webhooks/{hook['id']}", headers=admin_headers,
        json={"enabled": False, "events": ["RUN_FAILED", "RUN_SUCCEEDED"]},
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["enabled"] is False

    delete = await client.delete(f"/api/v1/run-webhooks/{hook['id']}", headers=admin_headers)
    assert delete.status_code == 200


async def test_run_webhook_requires_manage_permission(client, viewer_headers):
    resp = await client.post(
        "/api/run-webhooks", headers=viewer_headers,  # spec-path alias (no /v1)
        json={"name": "x", "url": "https://example.com/hook"},
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------ storage
async def test_kv_storage_roundtrip(client, admin_headers):
    put = await client.put(
        "/api/v1/storage/kv/actor-meta/site-config", headers=admin_headers,
        json={"value": {"sitemap": "https://x.test/sm.xml"}},
    )
    assert put.status_code == 200
    got = await client.get("/api/v1/storage/kv/actor-meta/site-config", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["data"]["value"]["sitemap"].endswith("sm.xml")
    listing = await client.get(
        "/api/v1/storage/kv", headers=admin_headers, params={"scope": "actor-meta"}
    )
    assert listing.json()["meta"]["total"] >= 1
    delete = await client.delete("/api/v1/storage/kv/actor-meta/site-config", headers=admin_headers)
    assert delete.status_code == 200
    gone = await client.get("/api/v1/storage/kv/actor-meta/site-config", headers=admin_headers)
    assert gone.status_code == 404


async def test_storage_queues_endpoint(client, admin_headers):
    resp = await client.get("/api/v1/storage/queues", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == []


# ------------------------------------------------------------------ change detection
async def test_change_detection_statuses(client, admin_headers, app):
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: F401
    from app.services.scraping.change_detection import compare_snapshots
    from app.services.scraping.datasets import DatasetService
    from app.models.actor_platform import DatasetStatus

    async with app.state.db.session() as session:
        svc = DatasetService(session)
        prev = await svc.create_for_job(job_id=None, actor_id="meta-ads-library", actor_version="1")
        await svc.add_items(prev.id, [
            {"change_key": "meta-ad:1", "content_hash": "aaaa"},
            {"change_key": "meta-ad:2", "content_hash": "bbbb"},
            {"change_key": "meta-ad:3", "content_hash": "cccc"},
        ])
        await svc.finalize(prev.id, status=DatasetStatus.READY)
        curr = await svc.create_for_job(job_id=None, actor_id="meta-ads-library", actor_version="1")
        await svc.add_items(curr.id, [
            {"change_key": "meta-ad:1", "content_hash": "aaaa"},   # unchanged
            {"change_key": "meta-ad:2", "content_hash": "zzzz"},   # modified
            {"change_key": "meta-ad:4", "content_hash": "dddd"},   # new
            # ad:3 missing → stopped
        ])
        await svc.finalize(curr.id, status=DatasetStatus.READY)
        await session.commit()
        changes = await compare_snapshots(session, prev.id, curr.id)
        by_status = {c["key"]: c["status"] for c in changes}
        assert by_status["meta-ad:1"] == "unchanged"
        assert by_status["meta-ad:2"] == "modified"
        assert by_status["meta-ad:3"] == "stopped"
        assert by_status["meta-ad:4"] == "new"


# ------------------------------------------------------------------ spec-path alias
async def test_spec_paths_without_version_prefix(client, admin_headers):
    """Spec §23 documents /api/... paths — aliases must work identically."""
    resp = await client.get("/api/actors", headers=admin_headers)
    assert resp.status_code == 200
    resp2 = await client.get("/api/tasks", headers=admin_headers)
    assert resp2.status_code == 200
    resp3 = await client.get("/api/runs", headers=admin_headers)
    assert resp3.status_code == 200
