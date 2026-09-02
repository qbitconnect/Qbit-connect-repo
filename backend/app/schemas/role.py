"""Role / permission schemas."""

from __future__ import annotations

from pydantic import BaseModel


class RoleOut(BaseModel):
    code: str
    name: str
    description: str | None
    permissions: list[str]


class RoleListOut(BaseModel):
    success: bool = True
    data: list[RoleOut]


class PermissionOut(BaseModel):
    code: str
    description: str | None


class PermissionListOut(BaseModel):
    success: bool = True
    data: list[PermissionOut]
