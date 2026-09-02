"""File schemas."""

from __future__ import annotations

from pydantic import BaseModel


class FileOut(BaseModel):
    id: str
    name: str
    mime_type: str | None
    size: int
    storage_backend: str
    category: str
    created_by: str | None
    created_at: str | None
    updated_at: str | None
    checksum_sha256: str | None
    metadata: dict
    is_deleted: bool = False


class FileListOut(BaseModel):
    success: bool = True
    data: list[FileOut]
    meta: dict


class FileStatsOut(BaseModel):
    success: bool = True
    data: dict


class FileActionOut(BaseModel):
    """Envelope for a single file record (upload/create responses)."""

    success: bool = True
    data: FileOut
