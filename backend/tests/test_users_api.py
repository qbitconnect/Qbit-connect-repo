"""User management API tests (Brief §9, §10, §31)."""

from __future__ import annotations

from tests.conftest import unique_email


async def test_create_user_as_admin(client, admin_headers):
    email = unique_email()
    resp = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "email": email,
            "password": "NewUserPass123",
            "full_name": "New Operator",
            "roles": ["OPERATOR"],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["email"] == email
    assert data["roles"] == ["OPERATOR"]
    assert "password" not in resp.text.lower() or "password_hash" not in resp.text


async def test_create_duplicate_email_conflict(client, admin_headers):
    payload = {
        "email": unique_email(),
        "password": "NewUserPass123",
        "roles": ["VIEWER"],
    }
    first = await client.post("/api/v1/users", headers=admin_headers, json=payload)
    assert first.status_code == 201
    dup = await client.post("/api/v1/users", headers=admin_headers, json=payload)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "CONFLICT"


async def test_create_user_forbidden_for_viewer(client, viewer_headers):
    resp = await client.post(
        "/api/v1/users",
        headers=viewer_headers,
        json={"email": unique_email(), "password": "NewUserPass123"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_list_users_paginated(client, admin_headers):
    for _ in range(3):
        await client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={"email": unique_email(), "password": "NewUserPass123", "roles": ["VIEWER"]},
        )
    resp = await client.get(
        "/api/v1/users", headers=admin_headers, params={"page": 1, "page_size": 2}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["meta"]["page"] == 1
    assert body["meta"]["total"] >= 4  # admin + viewer + 3 created


async def test_get_user_by_id(client, admin_headers):
    email = unique_email()
    created = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"email": email, "password": "NewUserPass123", "roles": ["MANAGER"]},
    )
    user_id = created.json()["data"]["id"]
    resp = await client.get(f"/api/v1/users/{user_id}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["email"] == email


async def test_patch_user_roles_and_activation(client, admin_headers):
    email = unique_email()
    created = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"email": email, "password": "NewUserPass123", "roles": ["VIEWER"]},
    )
    user_id = created.json()["data"]["id"]

    patched = await client.patch(
        f"/api/v1/users/{user_id}",
        headers=admin_headers,
        json={"roles": ["OPERATOR"], "is_active": True, "full_name": "Patched Name"},
    )
    assert patched.status_code == 200
    data = patched.json()["data"]
    assert data["roles"] == ["OPERATOR"]
    assert data["full_name"] == "Patched Name"


async def test_cannot_deactivate_self(client, admin_headers):
    me = await client.get("/api/v1/auth/me", headers=admin_headers)
    my_id = me.json()["data"]["id"]
    resp = await client.patch(
        f"/api/v1/users/{my_id}", headers=admin_headers, json={"is_active": False}
    )
    assert resp.status_code == 409


async def test_password_change_requires_diff(client, admin_headers):
    email = unique_email()
    created = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"email": email, "password": "OldPass123456", "roles": ["VIEWER"]},
    )
    user_id = created.json()["data"]["id"]
    same = await client.post(
        f"/api/v1/users/{user_id}/password",
        headers=admin_headers,
        json={"password": "OldPass123456"},
    )
    assert same.status_code == 409
    changed = await client.post(
        f"/api/v1/users/{user_id}/password",
        headers=admin_headers,
        json={"password": "NewPass987654"},
    )
    assert changed.status_code == 200
    # Old password no longer works; new one does.
    old_login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "OldPass123456"}
    )
    assert old_login.status_code == 401
    new_login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "NewPass987654"}
    )
    assert new_login.status_code == 200


async def test_unknown_user_404(client, admin_headers):
    resp = await client.get("/api/v1/users/00000000-0000-0000-0000-000000000000", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
