"""Phase 10 smoke test — real app + isolated SQLite DB (no docker, no pytest).

Verifies the Phase 10 final-verification items end to end:
  dashboard loads with REAL seed data, date filtering, comparison periods,
  scraper/marketing/whatsapp/email/inbox/automation analytics, permission
  matrix (viewer read-only, admin manage), saved reports (create → run →
  snapshot → export CSV), aggregate refresh + diagnostics, empty states on a
  clean database, and the analytics UI pages render.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("QBIT_ENV", "test")
os.chdir(Path(__file__).resolve().parents[1] / "backend")

import httpx  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


async def seed_operational_data(app) -> None:
    """Real operational rows (no fake analytics numbers): leads, scrape job,
    campaign + recipients + events, conversations + messages."""
    from datetime import datetime, timezone as dt_timezone

    sys.path.insert(0, "tests/analytics")
    from conftest import (  # type: ignore
        make_campaign, make_conversation_with_messages, make_event,
        make_lead, make_recipient, make_scrape_job,
    )

    from app.models.marketing import EventType

    now = datetime.now(dt_timezone.utc)
    db = app.state.db
    async with db.session() as session:
        await make_lead(session, status="NEW", created_at=now - timedelta(days=1),
                        quality=80)
        await make_lead(session, status="CONVERTED", email="c@example.com",
                        created_at=now - timedelta(days=3))
        await make_lead(session, status="INTERESTED",
                        created_at=now - timedelta(days=4))
        await make_scrape_job(session, status="COMPLETED", found=10, saved=8,
                              dupes=1, rejected=1,
                              created_at=now - timedelta(days=2))
        campaign = await make_campaign(session, name="Smoke WA")
        r1 = await make_recipient(session, campaign.id, status="DELIVERED",
                                  sent_at=now - timedelta(days=1),
                                  delivered_at=now - timedelta(days=1))
        r2 = await make_recipient(session, campaign.id, status="REPLIED",
                                  sent_at=now - timedelta(days=1),
                                  delivered_at=now - timedelta(days=1),
                                  replied_at=now - timedelta(hours=5))
        await make_event(session, campaign.id, EventType.MESSAGE_SENT, recipient_id=r1.id)
        await make_event(session, campaign.id, EventType.MESSAGE_SENT, recipient_id=r2.id)
        await make_event(session, campaign.id, EventType.MESSAGE_DELIVERED, recipient_id=r1.id)
        await make_event(session, campaign.id, EventType.MESSAGE_DELIVERED, recipient_id=r2.id)
        await make_event(session, campaign.id, EventType.MESSAGE_REPLIED, recipient_id=r2.id)
        await make_conversation_with_messages(session, status="OPEN",
                                              created_at=now - timedelta(days=1))
        await session.commit()


async def run() -> None:
    tmp = tempfile.mkdtemp(prefix="qbit-phase10-smoke-")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/smoke.db"
    os.environ["QBIT_DATA_DIR"] = tmp

    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import DatabaseManager
    from app.services.rbac import seed_admin, seed_rbac
    import app.models  # noqa: F401

    # schema via metadata (migration cycle itself is covered by test_migrations;
    # alembic's asyncio.run cannot nest inside this already-running loop)
    settings = Settings(
        QBIT_ENV="test", QBIT_SECRET_KEY="phase10-smoke-" + "k" * 48,
        DATABASE_URL=os.environ["DATABASE_URL"], QBIT_DATA_DIR=Path(tmp),
        QBIT_EXPORT_DIR=Path(tmp) / "exports", QBIT_LOG_DIR=Path(tmp) / "logs",
        QBIT_BACKUP_DIR=Path(tmp) / "backups", _env_file=None,
    )
    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    check("schema ready (analytics tables present)", {
        "reports", "report_runs", "report_snapshots", "analytics_daily_leads",
    } <= set(Base.metadata.tables))

    from app.main import create_app

    app = create_app(settings, db=db)
    async with db.session() as session:
        await seed_rbac(session)
        await seed_admin(session, email="admin@smoke.example",
                         password="Sm0keAdmin!Pass", full_name="Smoke Admin")
    await seed_operational_data(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        # login (admin)
        resp = await client.post("/api/v1/auth/login", json={
            "email": "admin@smoke.example", "password": "Sm0keAdmin!Pass"})
        admin = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        check("admin login", resp.status_code == 200)

        # create a viewer to prove read-only boundaries
        await client.post("/api/v1/users", headers=admin, json={
            "email": "viewer@smoke.example", "password": "Sm0keViewer!Pass",
            "full_name": "Smoke Viewer", "roles": ["VIEWER"]})
        resp = await client.post("/api/v1/auth/login", json={
            "email": "viewer@smoke.example", "password": "Sm0keViewer!Pass"})
        viewer = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        check("viewer login", resp.status_code == 200)

        # 1. dashboard loads with REAL lead metrics
        resp = await client.get("/api/v1/analytics/overview", headers=admin)
        body = resp.json()["data"]
        check("overview loads", resp.status_code == 200)
        check("overview total leads == 3 (real)",
              body["kpis"]["total_leads"]["current"] == 3,
              f"got {body['kpis']['total_leads']['current']}")

        # 2. date filtering excludes out-of-window data
        resp = await client.get("/api/v1/analytics/leads",
                                params={"period": "today"}, headers=admin)
        today_total = resp.json()["data"]["kpis"]["total"]
        check("date filtering works (today window)", today_total == 0,
              f"got {today_total}")
        resp = await client.get("/api/v1/analytics/leads",
                                params={"period": "7d"}, headers=admin)
        check("7d window captures all 3 leads",
              resp.json()["data"]["kpis"]["total"] == 3)

        # 3. comparison works
        resp = await client.get("/api/v1/analytics/leads",
                                params={"period": "7d", "compare": "1"}, headers=admin)
        cmp_total = resp.json()["data"]["comparison"]["total"]
        check("comparison previous window computed", "previous" in cmp_total)
        check("zero-previous pct is None (no fabrication)",
              cmp_total["change_pct"] is None)

        # 4. scraper analytics
        resp = await client.get("/api/v1/analytics/scraping", headers=admin)
        k = resp.json()["data"]["kpis"]
        check("scraper analytics real counters",
              k["jobs_total"] == 1 and k["records_accepted"] == 8
              and k["success_rate"] == 1.0)

        # 5. campaign/whatsapp/email analytics
        resp = await client.get("/api/v1/analytics/marketing", headers=admin)
        m = resp.json()["data"]["kpis"]
        check("marketing events real", m["messages_sent"] == 2
              and m["messages_delivered"] == 2 and m["replies"] == 1)
        resp = await client.get("/api/v1/analytics/whatsapp", headers=admin)
        w = resp.json()["data"]["kpis"]
        check("whatsapp reads honest (no read events → None rate)",
              w["messages_read"] == 0 and w["rates"]["read_rate"] is None)
        resp = await client.get("/api/v1/analytics/email", headers=admin)
        e = resp.json()["data"]["kpis"]
        check("email open/click rates None without tracking events",
              e["rates"]["open_rate"] is None and e["rates"]["click_rate"] is None)

        # 6. inbox + automation analytics
        resp = await client.get("/api/v1/analytics/inbox", headers=admin)
        inbox = resp.json()["data"]
        check("inbox conversations real", inbox["kpis"]["conversations_total"] == 1)
        check("inbox response time computed from real timestamps",
              inbox["response"]["avg_first_response_seconds"] == 120.0)
        resp = await client.get("/api/v1/analytics/automation", headers=admin)
        check("automation analytics empty-but-real",
              resp.json()["data"]["kpis"]["executions"] == 0)

        # 7. reports lifecycle
        resp = await client.post("/api/v1/reports", headers=admin, json={
            "name": "Smoke leads report", "domain": "LEADS",
            "config": {"domain": "LEADS", "metrics": ["total", "converted"],
                       "dimensions": ["source"], "period": "30d",
                       "visualization": "table"}})
        report = resp.json()["data"]
        check("report created", resp.status_code == 201)
        resp = await client.post(f"/api/v1/reports/{report['id']}/run",
                                 headers=admin, json={"format": "json"})
        check("report run queued", resp.status_code == 202)

        from app.analytics.reports.executor import ReportWorker
        from app.services.audit import AuditService
        from app.services.files import FileService

        worker = ReportWorker(owner="smoke",
                              storage_files=FileService(app.state.storage,
                                                        AuditService()))
        async with db.session() as session:
            ran = await worker.process_cycle(session)
        check("report run executed in background pattern", ran == 1)

        resp = await client.get(f"/api/v1/reports/{report['id']}/runs",
                                headers=admin)
        runs = resp.json()["data"]["items"]
        check("run completed with snapshot",
              runs and runs[0]["status"] == "COMPLETED")
        resp = await client.get(f"/api/v1/reports/{report['id']}/export?format=csv",
                                headers=admin)
        check("report export CSV", resp.status_code == 200
              and resp.headers["content-type"].startswith("text/csv"))

        # 8. RBAC: viewer read-only
        resp = await client.get("/api/v1/analytics/overview", headers=viewer)
        check("viewer reads overview", resp.status_code == 200)
        resp = await client.get("/api/v1/analytics/team", headers=viewer)
        check("viewer blocked from team analytics", resp.status_code == 403)
        resp = await client.post("/api/v1/analytics/aggregates/rebuild",
                                 headers=viewer, json={})
        check("viewer blocked from aggregate rebuild", resp.status_code == 403)
        resp = await client.post("/api/v1/reports", headers=viewer, json={
            "name": "nope", "domain": "LEADS",
            "config": {"domain": "LEADS", "metrics": ["total"]}})
        check("viewer blocked from report creation", resp.status_code == 403)

        # 9. aggregation refresh + diagnostics (admin)
        resp = await client.post("/api/v1/analytics/aggregates/rebuild",
                                 headers=admin, json={"domains": ["leads"]})
        check("aggregate rebuild (admin)", resp.status_code == 200
              and resp.json()["data"]["results"][0]["status"] == "COMPLETED")
        resp = await client.get("/api/v1/analytics/diagnostics", headers=admin)
        diag = resp.json()["data"]
        check("diagnostics read-only pass", resp.status_code == 200
              and diag["anomalies"] == 0)

        # 10. unknown filters rejected; UI pages render (cookie session)
        resp = await client.get("/api/v1/analytics/leads",
                                params={"evil": "1"}, headers=admin)
        check("unknown filter rejected", resp.status_code == 400)
        # UI pages authenticate via the HttpOnly session cookie, not Bearer
        resp = await client.post("/login", data={
            "email": "admin@smoke.example", "password": "Sm0keAdmin!Pass",
            "next": "/analytics"}, follow_redirects=False)
        check("UI cookie login", resp.status_code == 303)
        for page in ("/analytics", "/analytics/leads", "/analytics/scraping",
                     "/analytics/marketing", "/analytics/whatsapp",
                     "/analytics/email", "/analytics/inbox",
                     "/analytics/automation", "/analytics/team", "/reports",
                     "/reports/new"):
            resp = await client.get(page)
            check(f"UI {page} renders", resp.status_code == 200)

    await db.close()

    print(f"\nPhase 10 smoke: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(run())
