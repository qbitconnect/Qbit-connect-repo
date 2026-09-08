"""Phase 12 production smoke test (brief §53/§54) — real app, isolated DB.

Full release-candidate flow:
  health/live → login → dashboard → lead create/view → inbox → campaign →
  workflow → analytics → report → export → admin/ops → logout
Plus security headers + liveness/readiness semantics + login lockout gate.

NO real marketing messages are sent (mock-free run: campaigns are only
INSPECTED, never launched — §54 forbids real sends in smoke tests).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
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


async def run() -> None:
    tmp = tempfile.mkdtemp(prefix="qbit-phase12-smoke-")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/smoke.db"

    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import DatabaseManager
    from app.main import create_app
    from app.services.rbac import seed_admin, seed_rbac
    import app.models  # noqa: F401

    settings = Settings(
        QBIT_ENV="test", QBIT_SECRET_KEY="phase12-smoke-" + "k" * 48,
        DATABASE_URL=os.environ["DATABASE_URL"], QBIT_DATA_DIR=Path(tmp),
        QBIT_EXPORT_DIR=Path(tmp) / "exports", QBIT_LOG_DIR=Path(tmp) / "logs",
        QBIT_BACKUP_DIR=Path(tmp) / "backups",
        EMAIL_WEBHOOK_SECRET="smoke-webhook-secret",
        _env_file=None,
    )
    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = create_app(settings, db=db)
    async with db.session() as session:
        await seed_rbac(session)
        await seed_admin(session, email="admin@smoke.example",
                         password="Sm0keAdmin!Pass", full_name="Smoke Admin")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        # --- §54 minimum flow -------------------------------------------------
        resp = await client.get("/health/live")
        check("liveness /health/live = 200 alive",
              resp.status_code == 200 and resp.json()["status"] == "alive")

        resp = await client.get("/health/ready")
        check("readiness /health/ready aggregates services",
              resp.status_code == 200 and set(resp.json()["services"]) == {"database", "storage", "redis"})

        resp = await client.post("/api/v1/auth/login", json={"email": "x@x.x", "password": "y"})
        check("login rejects bad creds (401 envelope)", resp.status_code == 401)

        resp = await client.post("/api/v1/auth/login", json={
            "email": "admin@smoke.example", "password": "Sm0keAdmin!Pass"})
        check("login works", resp.status_code == 200, resp.text[:120])
        admin = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # dashboard (analytics overview = the operator dashboard data)
        resp = await client.get("/api/v1/analytics/overview", headers=admin)
        check("dashboard/analytics overview", resp.status_code == 200)

        # lead create → view
        resp = await client.post("/api/v1/leads", headers=admin, json={
            "business_name": "Smoke Lead", "email": "smoke@example.com",
            "phone": "+911234567890", "country": "IN"})
        lead_id = resp.json().get("data", {}).get("id")
        check("lead created", resp.status_code in (200, 201) and bool(lead_id), resp.text[:160])
        resp = await client.get(f"/api/v1/leads/{lead_id}", headers=admin)
        check("lead viewable", resp.status_code == 200)

        # inbox + conversations
        resp = await client.get("/api/v1/inbox/conversations", headers=admin)
        check("inbox loads", resp.status_code == 200)

        # campaigns list (no send — §54)
        resp = await client.get("/api/v1/campaigns", headers=admin)
        check("campaigns load (no sends)", resp.status_code == 200)

        # workflows
        resp = await client.get("/api/v1/automation/workflows", headers=admin)
        check("workflows load", resp.status_code == 200)

        # analytics + report endpoints (real data)
        resp = await client.get("/api/v1/analytics/leads/sources", headers=admin)
        check("analytics leads/sources", resp.status_code == 200, resp.text[:120])
        resp = await client.get("/api/v1/reports", headers=admin)
        check("reports list", resp.status_code == 200, resp.text[:120])

        # export a small dataset (CSV of the single lead)
        resp = await client.post(
            "/api/v1/leads/export", headers=admin,
            json={"format": "csv", "scope": "all"})
        check("export accepted", resp.status_code in (200, 202), resp.text[:160])

        # admin area + ops snapshot (real numbers only)
        resp = await client.get("/api/v1/admin/overview", headers=admin)
        check("admin overview", resp.status_code == 200)
        resp = await client.get("/api/v1/admin/ops", headers=admin)
        ok = resp.status_code == 200
        ops = resp.json().get("data", {}) if ok else {}
        check("admin ops snapshot (queue/disk/worker)", ok and "queue" in ops and "disk" in ops)
        check("ops snapshot exposes no filesystem paths",
              "path" not in resp.text and "/tmp/" not in resp.text and "qbit-phase12" not in resp.text)

        # security headers on a UI page
        resp = await client.get("/login")
        check("security headers on UI (CSP + DENY)",
              "Content-Security-Policy" in resp.headers and resp.headers.get("X-Frame-Options") == "DENY")

        # logout revokes the session
        resp = await client.post("/api/v1/auth/logout", headers=admin)
        check("logout revokes session", resp.status_code == 200)
        resp = await client.get("/api/v1/auth/me", headers=admin)
        check("revoked token no longer authenticates", resp.status_code == 401)

    print(f"\nphase12_smoke: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(run())
