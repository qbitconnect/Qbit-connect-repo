"""Auth schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str


class LogoutResponse(BaseModel):
    success: bool = True
    message: str
