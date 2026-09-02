"""System settings endpoints (Brief §11)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import DbSession, get_client_ip, get_user_agent
from app.api.deps import require_permission
from app.models.user import User
from app.schemas.setting import SettingOut, SettingUpdateRequest, SettingsOut
from app.services.settings import SystemSettingsService

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
async def get_settings_view(
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("settings.view"))],
):
    service = SystemSettingsService(request.app.state.audit)
    values = await service.get_all(session)
    return SettingsOut(data=values)


@router.get("/{key}", response_model=SettingOut)
async def get_setting(
    key: str,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("settings.view"))],
):
    service = SystemSettingsService(request.app.state.audit)
    value = await service.get(session, key)
    return SettingOut(data={"key": key, "value": value})


@router.put("/{key}", response_model=SettingOut)
async def update_setting(
    key: str,
    payload: SettingUpdateRequest,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("settings.manage"))],
):
    service = SystemSettingsService(request.app.state.audit)
    row = await service.set(session, key, payload.value, updated_by=actor.id)
    await request.app.state.audit.log(
        session,
        action="settings.updated",
        actor_user_id=actor.id,
        resource_type="system_setting",
        resource_id=key,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return SettingOut(data={"key": key, "value": row.value})
