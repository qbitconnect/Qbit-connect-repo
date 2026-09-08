"""File endpoints: upload / list / download / delete / stats (Brief §22, §24, §33).

Security: clients reference files by UUID only — raw paths are never accepted.
Downloads stream via FileResponse (constant memory, no full-RAM buffering).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import DbSession, FileServiceDep, get_client_ip, get_user_agent
from app.api.deps import require_permission
from app.core.config import Settings
from app.core.errors import (
    NotFoundError,
    PathAccessDeniedError,
    ValidationError,
)
from app.models.file import FileCategory, FileRecord
from app.models.user import User
from app.schemas.file import FileActionOut, FileListOut, FileOut, FileStatsOut
from app.services.audit import AuditService
from app.services.authorization import MemberContext, get_visible_or_404, visibility_clause

router = APIRouter(prefix="/files", tags=["files"])

ALLOWED_CATEGORIES = {c.value for c in FileCategory}


async def _ctx_for(session, user) -> MemberContext:
    """Resolve (or reuse) the Phase 11 member context for this request."""
    from app.services import authorization as authz
    from app.services import rbac as rbac_service

    perms = await rbac_service.load_user_permissions(session, user.id)
    return await authz.resolve_context(session, user, perms)


def _to_out(record: FileRecord) -> FileOut:
    return FileOut(**record.to_public_dict())


@router.post("", response_model=FileActionOut, status_code=201)
async def upload_file(
    request: Request,
    session: DbSession,
    files: FileServiceDep,
    upload: UploadFile,
    actor: Annotated[User, Depends(require_permission("files.create"))],
):
    settings: Settings = request.app.state.settings
    category = request.query_params.get("category", "OTHER").upper()
    if category not in ALLOWED_CATEGORIES:
        raise ValidationError(f"category must be one of: {sorted(ALLOWED_CATEGORIES)}")
    max_bytes = settings.QBIT_MAX_UPLOAD_MB * 1024 * 1024
    if upload.size is not None and upload.size > max_bytes:
        from starlette.exceptions import HTTPException

        raise HTTPException(status_code=413, detail="Uploaded file exceeds the size limit")
    # Phase 11 §24: uploads are stamped with the caller's organization so every
    # later download/delete can enforce the org boundary
    ctx = await _ctx_for(session, actor)
    record = await files.store(
        session,
        content=upload.file,
        filename=upload.filename or "upload.bin",
        mime_type=upload.content_type,
        category=category,
        created_by=actor.id,
        organization_id=ctx.organization_id,
        max_bytes=max_bytes,
    )
    return FileActionOut(data=_to_out(record))


@router.get("", response_model=FileListOut)
async def list_files(
    session: DbSession,
    files: FileServiceDep,
    actor: Annotated[User, Depends(require_permission("files.view"))],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    category: str | None = Query(default=None),
):
    # Phase 11 §9: file lists honor organization + visibility scope
    ctx = await _ctx_for(session, actor)
    rows, total = await files.list(
        session,
        category=category,
        page=page,
        page_size=page_size,
        extra_filter=visibility_clause(FileRecord, ctx),
    )
    return FileListOut(
        data=[_to_out(r) for r in rows],
        meta={"page": page, "page_size": page_size, "total": total},
    )


@router.get("/stats", response_model=FileStatsOut)
async def storage_stats(
    request: Request,
    session: DbSession,
    files: FileServiceDep,
    actor: Annotated[User, Depends(require_permission("files.view"))],
):
    """Admin storage view data (Brief §24) — app-scoped only, never the OS FS."""
    return FileStatsOut(data=files.storage.usage_summary())


@router.get("/{file_id}/download")
async def download_file(
    request: Request,
    file_id: uuid.UUID,
    session: DbSession,
    files: FileServiceDep,
    actor: Annotated[User, Depends(require_permission("exports.download"))],
):
    # Phase 11 §24 access chain: file_id → authentication (deps) → organization
    # check → permission (deps) → resource visibility check → stream download.
    # Foreign-org files answer 404 so IDs are never confirmed to strangers.
    ctx = await _ctx_for(session, actor)
    record = await get_visible_or_404(session, FileRecord, file_id, ctx)

    # EXPORT-category downloads additionally require exports.view (implied by role
    # matrix); audit every download (Brief §24).
    audit: AuditService = request.app.state.audit
    await audit.log(
        session,
        action="file.downloaded",
        actor_user_id=actor.id,
        resource_type="file",
        resource_id=str(record.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )

    try:
        path = files.filesystem_path(record)
    except PathAccessDeniedError:
        raise
    if not path.is_file():
        raise NotFoundError("File content missing from storage")

    return FileResponse(
        path=path,
        media_type=record.mime_type or "application/octet-stream",
        filename=record.name,
    )


@router.delete("/{file_id}", status_code=204)
async def delete_file(
    request: Request,
    file_id: uuid.UUID,
    session: DbSession,
    files: FileServiceDep,
    actor: Annotated[User, Depends(require_permission("files.delete"))],
):
    """Explicit, permissioned, audited deletion (Brief §26 — no hidden cleanup)."""
    # Phase 11 §24: deletion is also organization/visibility bounded
    ctx = await _ctx_for(session, actor)
    await get_visible_or_404(session, FileRecord, file_id, ctx)
    await files.delete(session, file_id, actor_user_id=actor.id)
