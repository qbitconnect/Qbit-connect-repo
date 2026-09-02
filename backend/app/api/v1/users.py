"""User management endpoints (Brief §9, §10).

RBAC is enforced server-side via `require_permission` dependencies — never only
by hiding UI elements.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.api.deps import DbSession, get_client_ip, get_user_agent
from app.api.deps import require_permission
from app.core.errors import ConflictError, NotFoundError
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.schemas.user import (
    UserActionOut,
    UserCreate,
    UserListOut,
    UserOut,
    UserPasswordChange,
    UserUpdate,
)
from app.services import rbac as rbac_service

router = APIRouter(prefix="/users", tags=["users"])


def _to_out(user: User) -> UserOut:
    return UserOut(**user.to_public_dict())


@router.post("", response_model=UserActionOut, status_code=201)
async def create_user(
    payload: UserCreate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.manage"))],
):
    email = payload.email.lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ConflictError("A user with this email already exists")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
    )
    session.add(user)
    await session.flush()

    if payload.roles:
        await rbac_service.set_user_roles(session, user.id, payload.roles)
    else:
        await session.commit()

    await session.refresh(user)
    await request.app.state.audit.log(
        session,
        action="user.created",
        actor_user_id=actor.id,
        resource_type="user",
        resource_id=str(user.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"roles": user.role_codes},
    )
    return UserActionOut(data=_to_out(user))


@router.get("", response_model=UserListOut)
async def list_users(
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.view"))],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    total = await session.scalar(select(func.count()).select_from(User))
    rows = await session.execute(
        select(User).order_by(User.created_at.asc()).offset((page - 1) * page_size).limit(page_size)
    )
    users = [_to_out(u) for u in rows.scalars().all()]
    return UserListOut(
        data=users,
        meta={"page": page, "page_size": page_size, "total": int(total or 0)},
    )


@router.get("/{user_id}", response_model=UserActionOut)
async def get_user(
    user_id: uuid.UUID,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.view"))],
):
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return UserActionOut(data=_to_out(user))


@router.patch("/{user_id}", response_model=UserActionOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.manage"))],
):
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")

    changed = {}
    if payload.full_name is not None:
        user.full_name = payload.full_name
        changed["full_name"] = payload.full_name
    if payload.is_active is not None:
        if user.id == actor.id and payload.is_active is False:
            from app.core.errors import ConflictError

            raise ConflictError("You cannot deactivate your own account")
        user.is_active = payload.is_active
        changed["is_active"] = payload.is_active
    await session.commit()

    if payload.roles is not None:
        applied = await rbac_service.set_user_roles(session, user.id, payload.roles)
        changed["roles"] = applied
        await session.refresh(user)

    await request.app.state.audit.log(
        session,
        action="user.updated",
        actor_user_id=actor.id,
        resource_type="user",
        resource_id=str(user.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"changes": changed},
    )
    return UserActionOut(data=_to_out(user))


@router.post("/{user_id}/password", response_model=UserActionOut)
async def change_password(
    user_id: uuid.UUID,
    payload: UserPasswordChange,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("users.manage"))],
):
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    if verify_password(payload.password, user.password_hash):
        raise ConflictError("New password must differ from the current password")
    user.password_hash = hash_password(payload.password)
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="user.password_changed",
        actor_user_id=actor.id,
        resource_type="user",
        resource_id=str(user.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return UserActionOut(data=_to_out(user))
