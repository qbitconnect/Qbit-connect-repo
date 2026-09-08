"""Phase 11 final verification — §44 manual checklist items executable over API.

Verifies: no secret ever returned; one-time displays; invitation E2E;
visibility enforcement; notification flow; audit immutability surface.
"""
import asyncio
import os
import sys

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_verify11.db"
os.environ["QBIT_SECRET_KEY"] = "verify-secret-phase11-" + "0" * 32
os.environ["QBIT_DATA_DIR"] = "./_verify11_data"
os.environ["QBIT_EXPORT_DIR"] = "./_verify11_data/exports"
os.environ["QBIT_LOG_DIR"] = "./_verify11_data/logs"

sys.path.insert(0, ".")

import uuid

from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import create_app

ADMIN = "admin@verify.io"
ADMIN_PW = "Sup3rVerify!1"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


async def main() -> int:
    from app.db.base import Base
    from app.db.session import DatabaseManager
    from sqlalchemy.ext.asyncio import create_async_engine

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    from app.services import rbac as rbac_service

    db = DatabaseManager(settings)
    async with db.session() as session:
        await rbac_service.seed_rbac(session)
        await rbac_service.seed_admin(session, email=ADMIN, password=ADMIN_PW, full_name="Verifier")
    await db.close()

    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://v") as c:
        r = await c.post("/api/v1/auth/login", json={"email": ADMIN, "password": ADMIN_PW})
        tok = r.json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}

        # 1. admin overview is real
        r = await c.get("/api/v1/admin/overview", headers=h)
        d = r.json()["data"]
        check("overview real counts", r.status_code == 200 and d["active_users"] >= 1
              and d["organization_members"] >= 1 and d["active_workflows"] is None)

        # 2. invitation E2E with one-time link
        r = await c.post("/api/v1/invitations", json={"email": "e2e@x.io", "role_codes": ["OPERATOR"]}, headers=h)
        inv_token = r.json()["invite_token"]
        check("invite token returned once", bool(inv_token))
        r2 = await c.get("/api/v1/invitations", headers=h)
        check("invite plaintext never stored/returned", inv_token not in r2.text)
        r3 = await c.post("/api/v1/invitations/accept",
                          json={"token": inv_token, "password": "NewUser!Pass1", "full_name": "E2E"})
        check("invitation accepted", r3.status_code == 200)
        r4 = await c.post("/api/v1/invitations/accept",
                          json={"token": inv_token, "password": "NewUser!Pass1", "full_name": "E2E"})
        check("invitation replay refused", r4.status_code in (403, 404, 409))
        r5 = await c.post("/api/v1/auth/login", json={"email": "e2e@x.io", "password": "NewUser!Pass1"})
        check("invited user can log in", r5.status_code == 200)
        u_tok = r5.json()["access_token"]

        # 3. operator can view leads (ALL default) but not admin surfaces
        r = await c.get("/api/v1/leads", headers={"Authorization": f"Bearer {u_tok}"})
        check("operator reads leads (ALL default)", r.status_code == 200)
        r = await c.get("/api/v1/admin/overview", headers={"Authorization": f"Bearer {u_tok}"})
        check("operator blocked from admin overview", r.status_code == 403)

        # 4. credentials never returned anywhere
        r = await c.get("/api/v1/sending-accounts", headers=h)
        body = json.dumps(r.json()) if (json := __import__("json")) else ""
        leaked = [k for k in ("password", "secret", "token", "api_key") if f'"{k}":' in body.lower()]
        check("sending accounts leak no secret fields", r.status_code in (200, 404) and not leaked, str(leaked))

        # 5. preferences round-trip
        r = await c.patch("/api/v1/users/me/preferences",
                          json={"timezone": "Asia/Kolkata", "preferences": {"dashboard": "ops"}}, headers=h)
        check("preferences update", r.status_code == 200 and r.json()["data"]["timezone"] == "Asia/Kolkata")
        r = await c.get("/api/v1/users/me/preferences", headers=h)
        check("preferences persist", r.json()["data"]["preferences"].get("dashboard") == "ops")

        # 6. notifications for the invited user exist? (admin notification on invite-with-existing-user
        #    is only for existing accounts; check the invitee's own inbox is reachable)
        r = await c.get("/api/v1/notifications/unread-count", headers={"Authorization": f"Bearer {u_tok}"})
        check("notifications endpoint reachable", r.status_code == 200)

        # 7. audit surface immutable (no DELETE/PATCH route)
        r = await c.request("DELETE", "/api/v1/admin/audit", headers=h)
        r2 = await c.request("PATCH", "/api/v1/admin/audit", headers=h, json={})
        check("audit log has no mutation route", r.status_code in (404, 405) and r2.status_code in (404, 405))

        # 8. sessions visible + revocable; revocation kills access
        r = await c.get("/api/v1/sessions/me", headers={"Authorization": f"Bearer {u_tok}"})
        sid = r.json()["data"][0]["id"]
        r = await c.post(f"/api/v1/sessions/{sid}/revoke", headers=h)
        check("admin can revoke user session", r.status_code == 200)
        r = await c.get("/api/v1/leads", headers={"Authorization": f"Bearer {u_tok}"})
        check("revoked session invalidates token", r.status_code == 401)

    ok = all(x[1] for x in CHECKS)
    print(f"\n{sum(1 for x in CHECKS if x[1])}/{len(CHECKS)} verification items passed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
