"""User schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    full_name: str | None = Field(default=None, max_length=200)
    roles: list[str] = Field(default_factory=list, max_length=5)


class UserUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    full_name: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None
    roles: list[str] | None = Field(default=None, max_length=5)


class UserPasswordChange(BaseModel):
    password: str = Field(min_length=10, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: EmailStr
    full_name: str | None
    is_active: bool
    roles: list[str]
    created_at: str | None
    updated_at: str | None
    last_login_at: str | None


class UserListOut(BaseModel):
    success: bool = True
    data: list[UserOut]
    meta: dict


class UserActionOut(BaseModel):
    success: bool = True
    data: UserOut
