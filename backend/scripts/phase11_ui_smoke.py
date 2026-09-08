"""Phase 11 UI smoke — renders every admin page over a real TestClient session."""
import os
import sys

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_smoke11.db"
os.environ["QBIT_SECRET_KEY"] = "smoke-secret-key-phase11-0123456789abcdef"
os.environ["QBIT_DATA_DIR"] = "./_smoke11_data"
os.environ["QBIT_EXPORT_DIR"] = "./_smoke11_data/exports"
os.environ["QBIT_LOG_DIR"] = "./_smoke11_data/logs"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"

import asyncio

sys.path.insert(0, ".")

from httpx import ASGITransport, AsyncClient

from alembic import command
from alembic.config import Config

from app.core.config import get_settings
from app.main import create_app


def _prepare() -> None:
    """Create schema directly (smoke test; migrations are covered by tests)."""
    import app.models  # noqa: F401 — register models on Base.metadata
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.base import Base as ModelBase

    async def _create():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        async with engine.begin() as conn:
            await conn.run_sync(ModelBase.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())

    async def _seed():
        from app.db.session import DatabaseManager
        from app.services import rbac as rbac_service

        settings = get_settings()
        db = DatabaseManager(settings)
        async with db.session() as session:
            await rbac_service.seed_rbac(session)
            await rbac_service.seed_admin(
                session, email="admin@x.io", password="Str0ngPass!x", full_name="Admin"
            )
        await db.close()

    asyncio.run(_seed())


async def main() -> int:
    settings = get_settings()
    app = create_app(settings)
    failures = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/auth/login", json={"email": "admin@x.io", "password": "Str0ngPass!x"}
        )
        if r.status_code != 200:
            print("login failed:", r.status_code, r.text[:300])
            return 1
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # UI login (cookie session), then render every admin page the way the
        # browser does — this is the real UI path
        r = await client.post(
            "/login", data={"email": "admin@x.io", "password": "Str0ngPass!x", "next": "/admin"},
            follow_redirects=False,
        )
        print("UI login ->", r.status_code)

        pages = [
            "/admin", "/admin/users", "/admin/teams", "/admin/roles",
            "/admin/invitations", "/admin/connections", "/admin/api-keys",
            "/admin/security", "/admin/audit", "/admin/settings",
            "/notifications", "/invite?token=x", "/login",
        ]
        for page in pages:
            r = await client.get(page, follow_redirects=False)
            status = r.status_code
            if status not in (200, 303, 307):
                failures.append((page, status, r.text[:300]))
            print(f"{page:28s} -> {status}")

        # team + invitation + api key flows through the UI forms
        r = await client.post("/admin/teams", data={"name": "Sales Outreach", "description": "Outbound"})
        print("create team ->", r.status_code)
        r = await client.get("/admin/teams")
        if "Sales Outreach" not in r.text:
            failures.append(("team visible", 0, r.text[:200]))
        r = await client.post("/admin/api-keys", data={"name": "CRM sync", "scopes": "leads.read"})
        if "Copy your key" not in r.text:
            failures.append(("apikey created page", 0, r.text[:400]))
        print("create api key page ->", r.status_code)
        r = await client.get("/admin/api-keys")
        if "CRM sync" not in r.text or "leads.read" not in r.text:
            failures.append(("apikey listed", 0, r.text[:300]))

    for f in failures:
        print("FAIL:", f)
    await asyncio.sleep(0)
    return 1 if failures else 0


if __name__ == "__main__":
    _prepare()
    raise SystemExit(asyncio.run(main()))
