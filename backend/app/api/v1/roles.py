"""Roles & permissions read endpoints (Brief §10)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api.deps import DbSession
from app.api.deps import require_permission
from app.models.rbac import Permission, Role
from app.models.user import User
from app.schemas.role import PermissionListOut, PermissionOut, RoleListOut, RoleOut

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get("", response_model=RoleListOut)
async def list_roles(
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("roles.view"))],
):
    rows = await session.execute(select(Role).order_by(Role.code.asc()))
    data = [
        RoleOut(
            code=role.code,
            name=role.name,
            description=role.description,
            permissions=role.permission_codes,
        )
        for role in rows.scalars().all()
    ]
    return RoleListOut(data=data)


@router.get("/permissions", response_model=PermissionListOut)
async def list_permissions(
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("roles.view"))],
):
    rows = await session.execute(select(Permission).order_by(Permission.code.asc()))
    return PermissionListOut(
        data=[PermissionOut(code=p.code, description=p.description) for p in rows.scalars().all()]
    )
