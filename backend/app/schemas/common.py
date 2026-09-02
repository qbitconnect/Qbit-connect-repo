"""Shared schema primitives."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int


class ErrorResponse(BaseModel):
    success: bool = False
    error: dict[str, Any]
