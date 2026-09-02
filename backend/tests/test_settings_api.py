"""System settings API tests (Brief §11)."""

from __future__ import annotations


async def test_get_settings_returns_defaults(client, admin_headers):
    resp = await client.get("/api/v1/settings", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["system.name"] == "QBIT Connect"
    assert data["system.timezone"] == "UTC"
    assert data["storage.default_backend"] == "local"


async def test_update_and_read_setting(client, admin_headers):
    put = await client.put(
        "/api/v1/settings/system.timezone",
        headers=admin_headers,
        json={"value": "Asia/Kolkata"},
    )
    assert put.status_code == 200
    got = await client.get("/api/v1/settings/system.timezone", headers=admin_headers)
    assert got.json()["data"]["value"] == "Asia/Kolkata"


async def test_settings_manage_permission_enforced(client, viewer_headers):
    got = await client.get("/api/v1/settings", headers=viewer_headers)
    assert got.status_code == 403

    put = await client.put(
        "/api/v1/settings/system.timezone",
        headers=viewer_headers,
        json={"value": "UTC+evil"},
    )
    assert put.status_code == 403
    assert put.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_sensitive_keys_rejected_in_settings(client, admin_headers):
    resp = await client.put(
        "/api/v1/settings/smtp_password",
        headers=admin_headers,
        json={"value": "hunter2"},
    )
    assert resp.status_code == 404
    assert "vault" in resp.json()["error"]["message"].lower()


async def test_setting_update_is_audited(client, admin_headers):
    from sqlalchemy import select

    from app.models.audit import AuditLog

    await client.put(
        "/api/v1/settings/system.name",
        headers=admin_headers,
        json={"value": "QBIT Ops"},
    )
    db = client._transport.app.state.db  # noqa: SLF001
    async with db.session() as session:
        rows = await session.execute(
            select(AuditLog).where(AuditLog.action == "settings.updated")
        )
        entries = rows.scalars().all()
    assert entries, "settings.updated must be audited"
