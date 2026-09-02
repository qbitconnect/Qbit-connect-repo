"""System settings schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SettingUpdateRequest(BaseModel):
    value: object = Field(...)


class SettingsOut(BaseModel):
    success: bool = True
    data: dict


class SettingOut(BaseModel):
    success: bool = True
    data: dict
