"""RBAC foundation tests (Brief §10)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import rbac as rbac_service


@pytest_asyncio.fixture
async def seeded_session(app):
    db = app.state.db
    async with db.session() as session:
        yield session


async def test_seed_rbac_is_idempotent(seeded_session: AsyncSession):
    counts1 = await rbac_service.seed_rbac(seeded_session)
    counts2 = await rbac_service.seed_rbac(seeded_session)
    assert counts1 == counts2 == {"roles": 5, "permissions": 34}


async def test_all_five_roles_exist(seeded_session: AsyncSession):
    from sqlalchemy import select

    from app.models.rbac import Role

    codes = set(await seeded_session.scalars(select(Role.code)))
    assert codes == {"SUPER_ADMIN", "ADMIN", "MANAGER", "OPERATOR", "VIEWER"}


async def test_super_admin_has_all_permissions(seeded_session: AsyncSession):
    from app.db.session import DatabaseManager

    from app.models.user import User

    # admin@qbit.example.com was seeded as SUPER_ADMIN in conftest
    from sqlalchemy import select

    admin = await seeded_session.scalar(select(User).where(User.email == "admin@qbit.example.com"))
    perms = await rbac_service.load_user_permissions(seeded_session, admin.id)
    assert perms >= {
        "users.manage", "settings.manage", "connections.manage",
        "campaign.create", "audit.view", "scraping.run",
    }
    assert len(perms) == len(rbac_service.PERMISSIONS)


async def test_viewer_is_read_only(seeded_session: AsyncSession):
    from sqlalchemy import select

    from app.models.user import User

    viewer = await seeded_session.scalar(select(User).where(User.email == "viewer@qbit.example.com"))
    perms = await rbac_service.load_user_permissions(seeded_session, viewer.id)
    assert "users.manage" not in perms
    assert "settings.manage" not in perms
    assert "files.delete" not in perms
    assert "exports.download" not in perms
    assert "files.view" in perms  # read-only allowed


async def test_set_user_roles_replaces_assignments(seeded_session: AsyncSession):
    from sqlalchemy import select

    from app.models.user import User

    viewer = await seeded_session.scalar(select(User).where(User.email == "viewer@qbit.example.com"))
    applied = await rbac_service.set_user_roles(seeded_session, viewer.id, ["OPERATOR", "VIEWER"])
    assert applied == ["OPERATOR", "VIEWER"]
    perms = await rbac_service.load_user_permissions(seeded_session, viewer.id)
    assert "scraping.run" in perms  # OPERATOR capability granted
    # restore
    await rbac_service.set_user_roles(seeded_session, viewer.id, ["VIEWER"])


async def test_unknown_role_rejected(seeded_session: AsyncSession):
    from sqlalchemy import select

    from app.core.errors import NotFoundError
    from app.models.user import User

    viewer = await seeded_session.scalar(select(User).where(User.email == "viewer@qbit.example.com"))
    with pytest.raises(NotFoundError):
        await rbac_service.set_user_roles(seeded_session, viewer.id, ["NOT_A_ROLE"])
