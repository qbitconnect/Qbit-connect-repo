"""Phase 6 tests — connections API (§36) + RBAC (§37) + secret redaction (§4/§42).

Multi-account architecture, validation flow, health checks, template sync,
credential exposure and IDOR protection — all against the app's real HTTP
surface with the gated whatsapp_mock provider (no network, §41).
"""

from __future__ import annotations

import pytest

from tests.marketing.conftest import make_lead

pytestmark = pytest.mark.asyncio


def _create_payload(**overrides) -> dict:
    payload = {
        "name": "QBIT WhatsApp 01",
        "provider": "whatsapp_mock",
        "phone_number_id": "111222333",
        "business_account_id": "999999999999",
        "credentials": {"access_token": "EAAG-test-token-value-0001"},
    }
    payload.update(overrides)
    return payload


async def _create_account(client, admin_headers, **overrides) -> dict:
    resp = await client.post(
        "/api/v1/connections/whatsapp", json=_create_payload(**overrides), headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


# ----------------------------------------------------------------- create
async def test_create_account_pending_and_masked(client, admin_headers):
    data = await _create_account(client, admin_headers)
    assert data["status"] == "PENDING"
    assert data["has_credentials"] is True
    assert "access_token" not in str(data)
    assert "EAAG" not in str(data)
    assert data["phone_number_id"] == "111222333"


async def test_create_multi_accounts_are_independent(client, admin_headers):
    """§3: multiple authorized sending accounts, no global single number."""
    first = await _create_account(client, admin_headers, name="QBIT WhatsApp 01")
    second = await _create_account(client, admin_headers, name="QBIT WhatsApp 02",
                                   phone_number_id="444555666")
    assert first["id"] != second["id"]
    assert first["phone_number_id"] != second["phone_number_id"]
    listing = await client.get("/api/v1/connections/whatsapp", headers=admin_headers)
    ids = [item["id"] for item in listing.json()["data"]["items"]]
    assert first["id"] in ids and second["id"] in ids


async def test_connections_overview_groups_channels(client, admin_headers):
    await _create_account(client, admin_headers)
    resp = await client.get("/api/v1/connections", headers=admin_headers)
    channels = resp.json()["data"]["channels"]
    assert "WHATSAPP" in channels and len(channels["WHATSAPP"]) == 1


# --------------------------------------------------------------- validation
async def test_validate_flow_activates_account(client, app, admin_headers, seeded_db):
    """§5: validation success → ACTIVE; failure → ERROR (never fake-ACTIVE)."""
    data = await _create_account(client, admin_headers)
    account_id = data["id"]
    assert data["status"] == "PENDING"

    resp = await client.post(f"/api/v1/connections/whatsapp/{account_id}/validate",
                             headers=admin_headers)
    data = resp.json()["data"]
    assert data["validation"]["ok"] is True
    assert data["status"] == "ACTIVE"
    assert data["provider_configured"] is True

    # a missing-credential account must NOT activate
    broken = await _create_account(client, admin_headers, name="No creds",
                                   phone_number_id="555000111",
                                   credentials={"access_token": "EAAG-nope-0000000002"})
    import uuid as uuid_mod

    from app.services.marketing.credentials import CredentialVault

    vault = CredentialVault(app.state.settings.QBIT_SECRET_KEY)
    # remove the vault row behind the app's back = broken credential reference
    cred_name = await _cred_name(seeded_db, broken["id"])
    row = await vault.get_row(seeded_db, name=cred_name)
    await vault.delete(seeded_db, name=row.name)
    resp = await client.post(f"/api/v1/connections/whatsapp/{broken['id']}/validate",
                             headers=admin_headers)
    data = resp.json()["data"]
    assert data["validation"]["ok"] is False
    assert data["status"] == "ERROR"


async def _cred_name(session, account_id: str) -> str:
    import uuid as uuid_mod

    from sqlalchemy import select

    from app.models.marketing import SendingAccount

    row = await session.scalar(
        select(SendingAccount.credential_ref).where(SendingAccount.id == uuid_mod.UUID(account_id))
    )
    assert row
    return row


# ------------------------------------------------------------------- health
async def test_health_check_updates_status(client, admin_headers):
    data = await _create_account(client, admin_headers)
    resp = await client.post(f"/api/v1/connections/whatsapp/{data['id']}/health",
                             headers=admin_headers)
    body = resp.json()["data"]
    assert body["health_detail"]["health"] == "HEALTHY"
    assert body["health_status"] == "HEALTHY"
    assert "access_token" not in str(body)


# ------------------------------------------------------------ template sync
async def test_template_sync_and_listing(client, admin_headers, seeded_db):
    data = await _create_account(client, admin_headers)
    resp = await client.post(
        f"/api/v1/connections/whatsapp/{data['id']}/sync-templates", headers=admin_headers,
    )
    summary = resp.json()["data"]
    assert summary["created"] == 4  # mock catalog: approved/pending/rejected/paused

    listing = await client.get(
        f"/api/v1/connections/whatsapp/{data['id']}/templates", headers=admin_headers,
    )
    templates = listing.json()["data"]["items"]
    statuses = {t["name"]: t["provider_status"] for t in templates}
    assert statuses["welcome_business"] == "APPROVED"
    assert statuses["order_update"] == "PENDING"
    assert statuses["spam_offer"] == "REJECTED"
    # §9: local custom metadata (variables mapping) preserved across re-sync
    from sqlalchemy import select

    from app.models.marketing import CampaignTemplate

    row = (await seeded_db.execute(
        select(CampaignTemplate).where(CampaignTemplate.provider_template_id == "tpl-approved-1")
    )).scalars().one()
    row.variables = ["first_name", "business_name"]
    await seeded_db.commit()
    resp = await client.post(
        f"/api/v1/connections/whatsapp/{data['id']}/sync-templates", headers=admin_headers,
    )
    assert resp.json()["data"]["updated"] == 4 and resp.json()["data"]["created"] == 0
    await seeded_db.refresh(row)
    assert row.variables == ["first_name", "business_name"]


# ------------------------------------------------------------------ update
async def test_patch_rotates_credentials_without_exposure(client, admin_headers):
    data = await _create_account(client, admin_headers)
    resp = await client.patch(
        f"/api/v1/connections/whatsapp/{data['id']}",
        json={"name": "QBIT WhatsApp 01 renamed",
              "credentials": {"access_token": "EAAG-rotated-token-value-0002"}},
        headers=admin_headers,
    )
    body = resp.json()["data"]
    assert body["name"] == "QBIT WhatsApp 01 renamed"
    assert body["has_credentials"] is True
    assert "EAAG-rotated" not in str(body)
    # rotation resets provider-validated state (must revalidate)
    assert body["provider_configured"] is False


async def test_delete_removes_account_and_credential(client, app, admin_headers, seeded_db):
    data = await _create_account(client, admin_headers)
    resp = await client.delete(f"/api/v1/connections/whatsapp/{data['id']}",
                               headers=admin_headers)
    assert resp.json()["data"]["removed"] is True
    from sqlalchemy import func, select

    from app.models.marketing import SendingAccount
    from app.models.messaging import ProviderCredentials

    count = await seeded_db.scalar(select(func.count()).select_from(SendingAccount))
    assert count == 0
    cred_count = await seeded_db.scalar(select(func.count()).select_from(ProviderCredentials))
    assert cred_count == 0


# -------------------------------------------------------------------- RBAC
async def test_viewer_cannot_create_or_validate(client, viewer_headers, admin_headers):
    resp = await client.post("/api/v1/connections/whatsapp",
                             json=_create_payload(), headers=viewer_headers)
    assert resp.status_code == 403
    created = await _create_account(client, admin_headers)
    for method, path in (
        ("post", f"/api/v1/connections/whatsapp/{created['id']}/validate"),
        ("post", f"/api/v1/connections/whatsapp/{created['id']}/health"),
        ("post", f"/api/v1/connections/whatsapp/{created['id']}/sync-templates"),
        ("patch", f"/api/v1/connections/whatsapp/{created['id']}"),
        ("delete", f"/api/v1/connections/whatsapp/{created['id']}"),
    ):
        resp = await client.request(method, path, headers=viewer_headers, json={})
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


async def test_viewer_can_view_connections(client, admin_headers, viewer_headers):
    await _create_account(client, admin_headers)
    resp = await client.get("/api/v1/connections", headers=viewer_headers)
    assert resp.status_code == 200


async def test_unauthenticated_requests_rejected(client):
    resp = await client.get("/api/v1/connections")
    assert resp.status_code == 401


# ----------------------------------------------------------------- security
async def test_unknown_account_returns_404(client, admin_headers):
    import uuid as uuid_mod

    resp = await client.get(f"/api/v1/connections/whatsapp/{uuid_mod.uuid4()}",
                            headers=admin_headers)
    assert resp.status_code == 404


async def test_secret_like_config_metadata_rejected(client, admin_headers):
    data = await _create_account(client, admin_headers)
    resp = await client.patch(
        f"/api/v1/connections/whatsapp/{data['id']}",
        json={"config_metadata": {"access_token": "EAAG-sneaky"}},
        headers=admin_headers,
    )
    assert resp.status_code in (400, 422)
    # the key NAME may appear in the rejection message — the VALUE must never
    assert "EAAG-sneaky" not in resp.text


async def test_sql_injection_in_path_param_is_safe(client, admin_headers):
    resp = await client.get("/api/v1/connections/whatsapp/not-a-uuid",
                            headers=admin_headers)
    assert resp.status_code == 422  # path validation rejects, no SQL executed


async def test_xss_in_account_name_is_escaped_in_ui(client, app, admin_headers):
    """UI renders Jinja autoescape — a script payload must not surface raw."""
    await _create_account(
        client, admin_headers,
        name="<script>alert(1)</script>WA",
        credentials={"access_token": "EAAG-xss-check-0000000099"},
    )
    # UI login (cookie session) → hub page
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    login = await client.post("/login", data={
        "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "next": "/connections",
    }, follow_redirects=False)
    assert login.status_code in (200, 303)
    resp = await client.get("/connections")
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


# -------------------------------------------------------------- §38 audit log
async def test_audit_records_whatsapp_actions_without_secrets(client, admin_headers, seeded_db):
    from sqlalchemy import select

    from app.models.audit import AuditLog

    data = await _create_account(client, admin_headers)
    await client.post(f"/api/v1/connections/whatsapp/{data['id']}/validate", headers=admin_headers)
    await client.post(f"/api/v1/connections/whatsapp/{data['id']}/health", headers=admin_headers)
    await client.post(f"/api/v1/connections/whatsapp/{data['id']}/sync-templates", headers=admin_headers)
    await client.patch(f"/api/v1/connections/whatsapp/{data['id']}",
                       json={"credentials": {"access_token": "EAAG-rotate-audit-0001"}},
                       headers=admin_headers)
    await client.delete(f"/api/v1/connections/whatsapp/{data['id']}", headers=admin_headers)

    rows = (await seeded_db.execute(
        select(AuditLog).where(AuditLog.resource_type == "sending_account")
        .order_by(AuditLog.created_at)
    )).scalars().all()
    actions = {row.action for row in rows}
    assert "whatsapp_account.created" in actions
    assert "whatsapp_account.credential_validated" in actions
    assert "whatsapp_account.health_checked" in actions
    assert "whatsapp_account.templates_synced" in actions
    assert "whatsapp_account.updated" in actions
    assert "whatsapp_account.removed" in actions
    # §38/§44: no credential values anywhere in the audit trail
    all_meta = str([row.action_metadata for row in rows if hasattr(row, "action_metadata")]) + str(
        [getattr(row, "metadata", None) for row in rows])
    assert "EAAG-test-token-value-0001" not in all_meta
    assert "EAAG-rotate-audit-0001" not in all_meta
