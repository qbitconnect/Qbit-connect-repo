"""Audit logging tests (Brief §12)."""

from __future__ import annotations

from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.user import User
from tests.conftest import ADMIN_EMAIL


async def _admin_user(session):
    return await session.scalar(select(User).where(User.email == ADMIN_EMAIL))


async def test_login_and_file_actions_audited(client, admin_headers):
    await client.post(
        "/api/v1/files", headers=admin_headers, files={"upload": ("audit.txt", b"x", "text/plain")}
    )

    db = client._transport.app.state.db  # noqa: SLF001
    async with db.session() as session:
        admin = await _admin_user(session)
        actions = set(
            await session.scalars(
                select(AuditLog.action).where(AuditLog.actor_user_id == admin.id)
            )
        )
    assert {"user.login", "file.created"} <= actions


async def test_file_delete_and_download_audited(client, admin_headers):
    up = await client.post(
        "/api/v1/files", headers=admin_headers, files={"upload": ("aud2.txt", b"y", "text/plain")}
    )
    fid = up.json()["data"]["id"]
    await client.get(f"/api/v1/files/{fid}/download", headers=admin_headers)
    await client.delete(f"/api/v1/files/{fid}", headers=admin_headers)

    db = client._transport.app.state.db  # noqa: SLF001
    async with db.session() as session:
        actions = set(await session.scalars(select(AuditLog.action)))
    assert {"file.downloaded", "file.deleted"} <= actions


async def test_audit_metadata_never_contains_secrets(client, admin_headers):
    """Secret-like metadata keys are redacted before persistence (Brief §12)."""
    db = client._transport.app.state.db  # noqa: SLF001
    audit = client._transport.app.state.audit  # noqa: SLF001
    async with db.session() as session:
        admin = await _admin_user(session)
        await audit.log(
            session,
            action="test.secret_probe",
            actor_user_id=admin.id,
            metadata={"password": "super-secret-value", "api_key": "abc123", "safe": "visible"},
        )
        row = await session.scalar(
            select(AuditLog).where(AuditLog.action == "test.secret_probe")
        )
    assert row.metadata_json["password"] == "***REDACTED***"
    assert row.metadata_json["api_key"] == "***REDACTED***"
    assert row.metadata_json["safe"] == "visible"
    assert "super-secret-value" not in str(row.metadata_json)


async def test_audit_survives_broken_metadata(client, admin_headers):
    """Audit failures must not break business operations (best-effort design)."""
    db = client._transport.app.state.db  # noqa: SLF001
    audit = client._transport.app.state.audit  # noqa: SLF001
    async with db.session() as session:
        admin = await _admin_user(session)
        # Non-serializable metadata is tolerated (redact/str fallback)
        await audit.log(
            session,
            action="test.weird",
            actor_user_id=admin.id,
            metadata={"obj": {1, 2, 3}},
        )
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "test.weird"))
    assert row is not None
