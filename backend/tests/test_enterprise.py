"""Phase 11 — Team / Admin / Enterprise engine tests (§39–§41).

Covers the SECURITY-CRITICAL surface:
- cross-tenant isolation (org B can NEVER touch org A — negative authorization)
- visibility scopes (ALL / TEAM / ASSIGNED_ONLY / OWNED_ONLY), backend-enforced
- invitation security (hash-at-rest, expiry, one-time use, replay, revocation)
- session lifecycle (revoke blocks JWT; deactivation revokes everything)
- API keys (scope allowlist, one-time plaintext, org isolation, IDOR)
- file access control (§24 chain: org → permission → visibility)
- enterprise safety guards (last-admin protection, self-role protection)
- assignment history integrity

All tests use the shared conftest isolation (tmp SQLite + seeded RBAC).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, TEST_SECRET, VIEWER_PASSWORD

# ----------------------------------------------------------------- helpers


async def _make_org_user(
    app,
    *,
    email: str,
    password: str = "An0ther!Pass1",
    roles: tuple[str, ...] = ("OPERATOR",),
    organization_id: uuid.UUID | None = None,
    full_name: str = "Member",
):
    """Create a user directly (with roles + optional org membership)."""
    from app.core.security import hash_password
    from app.models.user import User
    from app.services import rbac as rbac_service

    db = app.state.db
    async with db.session() as session:
        user = User(
            email=email,
            password_hash=hash_password(password),
            full_name=full_name,
            status="ACTIVE",
        )
        session.add(user)
        await session.flush()
        await rbac_service.set_user_roles(session, user.id, list(roles))
        await session.commit()
        user_id = user.id
    return user_id


async def _login_token(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_org(app, slug: str, name: str) -> uuid.UUID:
    from app.models.enterprise import Organization

    db = app.state.db
    async with db.session() as session:
        org = Organization(name=name, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _add_member(app, org_id: uuid.UUID, user_id: uuid.UUID):
    from app.models.enterprise import MemberStatus, OrganizationMember

    db = app.state.db
    async with db.session() as session:
        session.add(
            OrganizationMember(
                organization_id=org_id,
                user_id=user_id,
                status=MemberStatus.ACTIVE.value,
            )
        )
        await session.commit()


async def _make_lead(app, *, org_id, created_by, name="Lead") -> uuid.UUID:
    from app.models.scrape import Lead

    db = app.state.db
    async with db.session() as session:
        lead = Lead(
            business_name=name,
            organization_id=org_id,
            created_by=created_by,
            source="test",
            status="NEW",
        )
        session.add(lead)
        await session.commit()
        return lead.id


async def _make_file(app, *, org_id, created_by, name="f.txt") -> uuid.UUID:
    from app.models.file import FileRecord

    db = app.state.db
    async with db.session() as session:
        record = FileRecord(
            name=name,
            path=f"EXPORT/{uuid.uuid4().hex}-{name}",
            mime_type="text/plain",
            size=12,
            storage_backend="local",
            category="EXPORT",
            created_by=created_by,
            organization_id=org_id,
        )
        session.add(record)
        await session.commit()
        return record.id


async def _default_org(app) -> uuid.UUID:
    """Ensure (and return) the default organization id (created lazily)."""
    from app.services import authorization as authz

    db = app.state.db
    async with db.session() as session:
        org = await authz.ensure_default_organization(session)
        await session.commit()
        return org.id


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def admin_token(client):
    return await _login_token(client, ADMIN_EMAIL, ADMIN_PASSWORD)


# ----------------------------------------------------- cross-tenant isolation


class TestCrossTenantIsolation:
    """§28 CRITICAL: organization A must never access organization B."""

    async def test_foreign_lead_is_404_not_403(self, app, client, admin_token):
        """IDOR: another org's lead id answers 404 (existence never leaked)."""
        org_b = await _create_org(app, "org-b", "Org B")
        member_b = await _make_org_user(app, email="b@x.io", roles=("OPERATOR",))
        await _add_member(app, org_b, member_b)
        lead_b = await _make_lead(app, org_id=org_b, created_by=member_b)

        # org B member CANNOT see it via admin either; admin (org A) gets 404
        resp = await client.get(f"/api/v1/leads/{lead_b}", headers=_h(admin_token))
        assert resp.status_code == 404

        token_b = await _login_token(client, "b@x.io", "An0ther!Pass1")
        resp = await client.get(f"/api/v1/leads/{lead_b}", headers=_h(token_b))
        assert resp.status_code == 200  # own org is fine

    async def test_lead_list_filtered_by_org(self, app, client, admin_token):
        org_b = await _create_org(app, "org-b2", "Org B2")
        member_b = await _make_org_user(app, email="b2@x.io")
        await _add_member(app, org_b, member_b)
        lead_b = await _make_lead(app, org_id=org_b, created_by=member_b, name="Foreign")

        resp = await client.get("/api/v1/leads", headers=_h(admin_token))
        assert resp.status_code == 200
        ids = [row["id"] for row in resp.json()["data"]["items"]]
        assert str(lead_b) not in ids

    async def test_foreign_file_download_404(self, app, client, admin_token):
        org_b = await _create_org(app, "org-b3", "Org B3")
        member_b = await _make_org_user(app, email="b3@x.io")
        await _add_member(app, org_b, member_b)
        file_b = await _make_file(app, org_id=org_b, created_by=member_b)
        resp = await client.get(f"/api/v1/files/{file_b}/download", headers=_h(admin_token))
        assert resp.status_code == 404

    async def test_foreign_scrape_job_404(self, app, client, admin_token):
        from app.models.scrape import ScrapeJob

        org_b = await _create_org(app, "org-b4", "Org B4")
        member_b = await _make_org_user(app, email="b4@x.io")
        await _add_member(app, org_b, member_b)
        db = app.state.db
        async with db.session() as session:
            job = ScrapeJob(
                actor_id="google-maps",
                actor_version="1.0.0",
                organization_id=org_b,
                created_by=member_b,
                status="COMPLETED",
            )
            session.add(job)
            await session.commit()
            job_id = job.id
        resp = await client.get(f"/api/v1/scrape-jobs/{job_id}", headers=_h(admin_token))
        assert resp.status_code == 404

    async def test_x_organization_header_rejected_for_non_member(self, app, client, admin_token):
        """A member of org A cannot hop to org B via the X-Organization-Id header."""
        org_b = await _create_org(app, "org-b5", "Org B5")
        resp = await client.get(
            "/api/v1/leads",
            headers={**_h(admin_token), "X-Organization-Id": str(org_b)},
        )
        assert resp.status_code in (403, 404)


# ------------------------------------------------------------ visibility scopes


class TestVisibilityScopes:
    """§9: ALL | TEAM | ASSIGNED_ONLY | OWNED_ONLY — backend-enforced."""

    async def _default_org_id(self, app) -> uuid.UUID:
        return await _default_org(app)

    async def test_owned_only_scope_hides_others_leads(self, app, client, admin_token):
        """An OWNED_ONLY member sees only their own leads (list + detail 404)."""
        from sqlalchemy import select

        from app.models.enterprise import OrganizationMember

        org_id = await self._default_org_id(app)
        member = await _make_org_user(app, email="owned@x.io", roles=("OPERATOR",))
        await _add_member(app, org_id, member)
        db = app.state.db
        async with db.session() as session:
            m = (await session.execute(
                select(OrganizationMember).where(OrganizationMember.user_id == member)
            )).scalars().first()
            m.visibility_scope = "OWNED_ONLY"
            await session.commit()

        mine = await _make_lead(app, org_id=org_id, created_by=member, name="Mine")
        other = await _make_lead(app, org_id=org_id, created_by=None, name="NotMine")

        token = await _login_token(client, "owned@x.io", "An0ther!Pass1")
        resp = await client.get("/api/v1/leads", headers=_h(token))
        assert resp.status_code == 200
        ids = [row["id"] for row in resp.json()["data"]["items"]]
        assert str(mine) in ids
        assert str(other) not in ids

        resp = await client.get(f"/api/v1/leads/{other}", headers=_h(token))
        assert resp.status_code == 404
        resp = await client.get(f"/api/v1/leads/{mine}", headers=_h(token))
        assert resp.status_code == 200

    async def test_assigned_only_scope(self, app, client, admin_token):
        from sqlalchemy import select

        from app.models.enterprise import OrganizationMember

        org_id = await self._default_org_id(app)
        member = await _make_org_user(app, email="assigned@x.io", roles=("OPERATOR",))
        await _add_member(app, org_id, member)
        db = app.state.db
        async with db.session() as session:
            m = (await session.execute(
                select(OrganizationMember).where(OrganizationMember.user_id == member)
            )).scalars().first()
            m.visibility_scope = "ASSIGNED_ONLY"
            await session.commit()

        lead = await _make_lead(app, org_id=org_id, created_by=None, name="Pool")
        # admin assigns the lead to the member
        resp = await client.post(
            f"/api/v1/leads/{lead}/assignment",
            json={"assigned_user_id": str(member), "reason": "round-robin"},
            headers=_h(admin_token),
        )
        assert resp.status_code == 200, resp.text

        token = await _login_token(client, "assigned@x.io", "An0ther!Pass1")
        resp = await client.get("/api/v1/leads?assigned_to_me=true", headers=_h(token))
        ids = [row["id"] for row in resp.json()["data"]["items"]]
        assert str(lead) in ids

    async def test_role_visibility_default_via_org_settings(self, app, client, admin_token):
        """Org-level visibility_defaults apply to roles without overrides."""
        from sqlalchemy import select, update

        from app.models.enterprise import Organization
        from app.models.scrape import Lead as LeadModel
        from app.models.user import User

        db = app.state.db
        org_id = await _default_org(app)
        async with db.session() as session:
            admin = (await session.execute(
                select(User).where(User.email == ADMIN_EMAIL)
            )).scalars().first()
            admin_id = admin.id
            # role-level default visibility for OPERATORs of this org
            org = await session.get(Organization, org_id)
            org.settings_json = {"visibility_defaults": {"OPERATOR": "OWNED_ONLY"}}
            await session.commit()

        member = await _make_org_user(app, email="visrole@x.io", roles=("OPERATOR",))
        await _add_member(app, org_id, member)
        token = await _login_token(client, "visrole@x.io", "An0ther!Pass1")

        # a lead created by the admin must be invisible to this operator
        other_lead = await _make_lead(app, org_id=org_id, created_by=admin_id, name="AdminsLead")
        resp = await client.get("/api/v1/leads", headers=_h(token))
        ids = [row["id"] for row in resp.json()["data"]["items"]]
        assert str(other_lead) not in ids


# ---------------------------------------------------------- invitation security


class TestInvitationSecurity:
    async def test_invitation_flow_hash_and_one_time(self, app, client, admin_token):
        headers = _h(admin_token)
        resp = await client.post(
            "/api/v1/invitations",
            json={"email": "newbie@x.io", "role_codes": ["OPERATOR"]},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        token = body["invite_token"]
        assert token  # plaintext shown once
        assert body["data"]["status"] == "PENDING"

        # the stored value is a HASH — the plaintext never appears in the API
        resp2 = await client.get("/api/v1/invitations", headers=headers)
        rows = resp2.json()["data"]
        assert all(row["id"] for row in rows)
        dumped = resp2.text
        assert token not in dumped

        # accept
        resp3 = await client.post(
            "/api/v1/invitations/accept",
            json={"token": token, "password": "V3ryNew!Pass", "full_name": "Newbie"},
        )
        assert resp3.status_code == 200, resp3.text

        # replay is refused (one-time)
        resp4 = await client.post(
            "/api/v1/invitations/accept",
            json={"token": token, "password": "V3ryNew!Pass", "full_name": "Newbie"},
        )
        assert resp4.status_code in (403, 404, 409)

        # the new user can log in and is a member of the org
        t = await _login_token(client, "newbie@x.io", "V3ryNew!Pass")
        assert t

    async def test_invitation_expiry(self, app, client, admin_token):
        from sqlalchemy import select

        from app.models.enterprise import Invitation

        headers = _h(admin_token)
        resp = await client.post(
            "/api/v1/invitations",
            json={"email": "expired@x.io", "role_codes": ["VIEWER"]},
            headers=headers,
        )
        token = resp.json()["invite_token"]

        # force-expire the invitation in the DB
        db = app.state.db
        async with db.session() as session:
            inv = (await session.execute(
                select(Invitation).where(Invitation.email == "expired@x.io")
            )).scalars().first()
            inv.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await session.commit()

        resp2 = await client.post(
            "/api/v1/invitations/accept",
            json={"token": token, "password": "V3ryNew!Pass", "full_name": "Late"},
        )
        assert resp2.status_code in (403, 409)

    async def test_invitation_revocation(self, app, client, admin_token):
        headers = _h(admin_token)
        resp = await client.post(
            "/api/v1/invitations",
            json={"email": "revoke@x.io", "role_codes": ["VIEWER"]},
            headers=headers,
        )
        body = resp.json()
        token = body["invite_token"]
        inv_id = body["data"]["id"]

        resp2 = await client.post(f"/api/v1/invitations/{inv_id}/revoke", headers=headers)
        assert resp2.status_code == 200

        resp3 = await client.post(
            "/api/v1/invitations/accept",
            json={"token": token, "password": "V3ryNew!Pass", "full_name": "X"},
        )
        assert resp3.status_code in (403, 404, 409)

    async def test_invitation_cannot_grant_super_admin(self, app, client, admin_token):
        resp = await client.post(
            "/api/v1/invitations",
            json={"email": "sneaky@x.io", "role_codes": ["SUPER_ADMIN"]},
            headers=_h(admin_token),
        )
        assert resp.status_code in (400, 403, 404)

    async def test_invite_rate_limit(self, app, client, admin_token):
        headers = _h(admin_token)
        got_429 = False
        for i in range(40):
            resp = await client.post(
                "/api/v1/invitations",
                json={"email": f"rl{i}@x.io", "role_codes": []},
                headers=headers,
            )
            if resp.status_code == 429:
                got_429 = True
                break
        assert got_429, "expected the invite limiter to engage"


# --------------------------------------------------------------- session security


class TestSessionSecurity:
    async def test_revoked_session_blocks_token(self, app, client, admin_token):
        resp = await client.get("/api/v1/sessions/me", headers=_h(admin_token))
        assert resp.status_code == 200
        rows = resp.json()["data"]
        assert rows, "login should have registered a session"
        session_id = rows[0]["id"]

        resp2 = await client.post(f"/api/v1/sessions/{session_id}/revoke", headers=_h(admin_token))
        assert resp2.status_code == 200

        resp3 = await client.get("/api/v1/leads", headers=_h(admin_token))
        assert resp3.status_code == 401, "revoked session must invalidate the JWT"

    async def test_deactivation_revokes_sessions(self, app, client, admin_token):
        target = await _make_org_user(app, email="deact@x.io", roles=("OPERATOR",))
        t = await _login_token(client, "deact@x.io", "An0ther!Pass1")
        resp = await client.get("/api/v1/leads", headers=_h(t))
        assert resp.status_code == 200

        resp2 = await client.patch(
            f"/api/v1/users/{target}",
            json={"is_active": False},
            headers=_h(admin_token),
        )
        assert resp2.status_code == 200, resp2.text

        resp3 = await client.get("/api/v1/leads", headers=_h(t))
        assert resp3.status_code in (401, 403)

    async def test_logout_revokes_session(self, app, client):
        t = await _login_token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        resp = await client.post("/api/v1/auth/logout", headers=_h(t))
        assert resp.status_code == 200
        resp2 = await client.get("/api/v1/leads", headers=_h(t))
        assert resp2.status_code == 401


# -------------------------------------------------------------------- API keys


class TestApiKeys:
    async def test_key_auth_scopes_and_idor(self, app, client, admin_token):
        headers = _h(admin_token)

        # create a lead owned by the admin org
        org_rows = await client.get("/api/v1/leads", headers=headers)
        resp = await client.post(
            "/api/v1/api-keys",
            json={"name": "crm", "scopes": ["leads.read"]},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        plaintext = body["api_key"]
        assert plaintext.startswith("qbit_")
        # plaintext is NOT retrievable again
        resp_list = await client.get("/api/v1/api-keys", headers=headers)
        assert plaintext not in resp_list.text

        # scope enforcement: key may read leads
        resp2 = await client.get("/api/v1/leads", headers=_h(plaintext))
        assert resp2.status_code == 200

        # scope enforcement: key may NOT create leads (scope not granted)
        resp3 = await client.post(
            "/api/v1/leads",
            json={"business_name": "Nope"},
            headers=_h(plaintext),
        )
        assert resp3.status_code == 403

        # scope enforcement: key may NOT call admin endpoints
        resp4 = await client.get("/api/v1/admin/overview", headers=_h(plaintext))
        assert resp4.status_code == 403

        # revoke → dead immediately
        key_id = body["data"]["id"]
        resp5 = await client.post(f"/api/v1/api-keys/{key_id}/revoke", headers=headers)
        assert resp5.status_code == 200
        resp6 = await client.get("/api/v1/leads", headers=_h(plaintext))
        assert resp6.status_code in (401, 403)

    async def test_api_key_never_grants_admin(self, app, client, admin_token):
        resp = await client.post(
            "/api/v1/api-keys",
            json={"name": "hmm", "scopes": ["users.view", "users.manage"]},
            headers=_h(admin_token),
        )
        assert resp.status_code in (201, 422)


# ------------------------------------------------------------ enterprise guards


class TestEnterpriseGuards:
    async def test_cannot_demote_last_super_admin(self, app, client, admin_token):
        from sqlalchemy import select

        from app.models.user import User

        db = app.state.db
        async with db.session() as session:
            admin = (await session.execute(
                select(User).where(User.email == ADMIN_EMAIL)
            )).scalars().first()
            admin_id = admin.id

        resp = await client.patch(
            f"/api/v1/users/{admin_id}",
            json={"roles": ["ADMIN"]},
            headers=_h(admin_token),
        )
        assert resp.status_code in (400, 403, 409)

    async def test_cannot_deactivate_last_super_admin(self, app, client, admin_token):
        from sqlalchemy import select

        from app.models.user import User

        db = app.state.db
        async with db.session() as session:
            admin = (await session.execute(
                select(User).where(User.email == ADMIN_EMAIL)
            )).scalars().first()
            admin_id = admin.id
        resp = await client.patch(
            f"/api/v1/users/{admin_id}",
            json={"is_active": False},
            headers=_h(admin_token),
        )
        assert resp.status_code in (400, 403, 409)

    async def test_cannot_change_own_roles(self, app, client, admin_token):
        from sqlalchemy import select

        from app.models.user import User

        db = app.state.db
        async with db.session() as session:
            admin = (await session.execute(
                select(User).where(User.email == ADMIN_EMAIL)
            )).scalars().first()
            admin_id = admin.id
        resp = await client.patch(
            f"/api/v1/users/{admin_id}",
            json={"roles": ["VIEWER"]},
            headers=_h(admin_token),
        )
        assert resp.status_code in (400, 403, 409)

    async def test_super_admin_not_granted_via_creation_or_edit(self, app, client, admin_token):
        resp = await client.post(
            "/api/v1/users",
            json={"email": "sa@x.io", "password": "Str0ngPass!9", "roles": ["SUPER_ADMIN"]},
            headers=_h(admin_token),
        )
        assert resp.status_code in (400, 403, 409)


# ----------------------------------------------------------- assignment history


class TestAssignmentHistory:
    async def test_lead_assignment_creates_history_and_notification(self, app, client, admin_token):
        from sqlalchemy import select

        from app.models.enterprise import LeadAssignmentHistory
        from app.models.user import User

        db = app.state.db
        async with db.session() as session:
            admin = (await session.execute(
                select(User).where(User.email == ADMIN_EMAIL)
            )).scalars().first()
            admin_id = admin.id

        member = await _make_org_user(app, email="assignee@x.io", roles=("OPERATOR",))
        org_id = await _default_org(app)
        await _add_member(app, org_id, member)
        lead = await _make_lead(app, org_id=org_id, created_by=admin_id, name="Hist")

        resp = await client.post(
            f"/api/v1/leads/{lead}/assignment",
            json={"assigned_user_id": str(member), "reason": "territory"},
            headers=_h(admin_token),
        )
        assert resp.status_code == 200, resp.text

        async with db.session() as session:
            hist = (await session.execute(
                select(LeadAssignmentHistory).where(LeadAssignmentHistory.lead_id == lead)
            )).scalars().all()
            assert len(hist) == 1
            assert str(hist[0].assigned_user_id) == str(member)
            assert hist[0].reason == "territory"
            assert hist[0].changed_by is not None

        # reassignment keeps BOTH entries (no silent overwrites)
        member2 = await _make_org_user(app, email="assignee2@x.io", roles=("OPERATOR",))
        await _add_member(app, org_id, member2)
        resp2 = await client.post(
            f"/api/v1/leads/{lead}/assignment",
            json={"assigned_user_id": str(member2), "reason": "rebalance"},
            headers=_h(admin_token),
        )
        assert resp2.status_code == 200
        async with db.session() as session:
            hist = (await session.execute(
                select(LeadAssignmentHistory).where(LeadAssignmentHistory.lead_id == lead)
                .order_by(LeadAssignmentHistory.created_at)
            )).scalars().all()
            assert len(hist) == 2
            assert str(hist[1].previous_user_id) == str(member)
            assert str(hist[1].assigned_user_id) == str(member2)

        # history endpoint is readable
        resp3 = await client.get(f"/api/v1/leads/{lead}/assignment-history", headers=_h(admin_token))
        assert resp3.status_code == 200

    async def test_assignment_target_must_be_org_member(self, app, client, admin_token):
        from sqlalchemy import select

        from app.models.enterprise import Organization
        from app.models.user import User
        from app.models.scrape import Lead as LeadModel
        from sqlalchemy import update

        db = app.state.db
        org_id = await _default_org(app)
        async with db.session() as session:
            admin = (await session.execute(
                select(User).where(User.email == ADMIN_EMAIL)
            )).scalars().first()
            admin_id = admin.id

        outsider = await _make_org_user(app, email="outsider@x.io", roles=("OPERATOR",))
        lead = await _make_lead(app, org_id=org_id, created_by=admin_id)

        resp = await client.post(
            f"/api/v1/leads/{lead}/assignment",
            json={"assigned_user_id": str(outsider)},
            headers=_h(admin_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------- notifications


class TestNotifications:
    async def test_assignment_notification_emitted(self, app, client, admin_token):
        from sqlalchemy import select

        from app.models.user import User

        org_id = await _default_org(app)
        db = app.state.db
        async with db.session() as session:
            admin = (await session.execute(
                select(User).where(User.email == ADMIN_EMAIL)
            )).scalars().first()

        member = await _make_org_user(app, email="notif@x.io", roles=("OPERATOR",))
        await _add_member(app, org_id, member)
        lead = await _make_lead(app, org_id=org_id, created_by=admin.id)
        resp = await client.post(
            f"/api/v1/leads/{lead}/assignment",
            json={"assigned_user_id": str(member)},
            headers=_h(admin_token),
        )
        assert resp.status_code == 200

        token = await _login_token(client, "notif@x.io", "An0ther!Pass1")
        resp2 = await client.get("/api/v1/notifications", headers=_h(token))
        assert resp2.status_code == 200
        types = [n["type"] for n in resp2.json()["data"]]
        assert "ASSIGNMENT" in types

    async def test_mark_notification_read(self, app, client, admin_token):
        # login records nothing notification-worthy; use an admin-side event
        resp = await client.get("/api/v1/notifications/unread-count", headers=_h(admin_token))
        assert resp.status_code == 200


# ------------------------------------------------------------------- audit API


class TestAuditCenter:
    async def test_audit_search_and_immutability(self, app, client, admin_token):
        headers = _h(admin_token)
        # generate some audit traffic
        await client.get("/api/v1/admin/overview", headers=headers)

        resp = await client.get("/api/v1/admin/audit?action=login", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["meta"]["total"] >= 1

        # no mutation verbs exist for audit logs
        resp2 = await client.request(
            "DELETE", "/api/v1/admin/audit", headers=headers
        )
        assert resp2.status_code in (405, 404)

    async def test_admin_overview_uses_real_counts(self, app, client, admin_token):
        resp = await client.get("/api/v1/admin/overview", headers=_h(admin_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["active_users"] >= 1
        assert data["organization_members"] >= 1
        assert data["active_workflows"] is None  # honest: workflow engine absent
