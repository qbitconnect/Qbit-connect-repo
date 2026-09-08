"""Lead workspace REST API (Phase 4 §30-§32).

Every endpoint enforces RBAC server-side (require_permission). Pagination is
always bounded (max page size from settings); filters and sorts are validated
against server-side whitelists before touching SQL.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditDep, DbSession, FileServiceDep, require_permission
from app.core.config import Settings
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.models.lead import (
    DuplicateStatus,
    ImportBatch,
    LeadDuplicateCandidate,
    LeadExportRecord,
    LeadTag,
    SavedView,
)
from app.models.scrape import Lead
from app.models.user import User
from app.schemas.leads import (
    BulkActionRequest,
    DuplicateResolveRequest,
    ExportRequest,
    ImportMappingRequest,
    LeadCreate,
    LeadTagAssignRequest,
    LeadUpdate,
    MergeRequest,
    NoteCreate,
    SavedViewCreate,
    SavedViewUpdate,
    StatusChangeRequest,
    TagCreate,
    TagUpdate,
)
from app.services import rbac as rbac_service
from app.services.files import FileService
from app.services.leads import (
    DuplicateDetectionService,
    LeadWorkspaceService,
    MergeService,
    SavedViewService,
    TagService,
)
from app.services.leads.activity import LeadActivityService
from app.services.leads.exporter import LeadExportService
from app.services.leads.importer import LeadImportService

logger = get_logger("qbit.api.leads")

async def _ctx_for(session, user) -> "MemberContext":
    """Resolve (or reuse) the Phase 11 member context for this request."""
    from app.services import authorization as authz

    perms = getattr(user, "_qbit_perms", None)
    if perms is None:
        from app.services import rbac as rbac_service

        perms = await rbac_service.load_user_permissions(session, user.id)
    return await authz.resolve_context(session, user, perms)


async def _visible_lead(session, lead_id: uuid.UUID, user):
    """IDOR-safe lead fetch: organization + visibility scope, 404 on foreign."""
    from app.services import authorization as authz

    ctx = await _ctx_for(session, user)
    return await authz.get_visible_or_404(session, Lead, lead_id, ctx)



router = APIRouter(prefix="/leads", tags=["leads"])

workspace = LeadWorkspaceService()
tag_service = TagService()
view_service = SavedViewService()
duplicates_service = DuplicateDetectionService()
merge_service = MergeService()
activities = LeadActivityService()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _page_envelope(items: list, total: int, page: int, page_size: int) -> dict:
    return {
        "success": True,
        "data": {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, -(-total // page_size)) if total else 1,
        },
    }


def _parse_filters(raw: str | None) -> dict | list | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValidationError("filters must be valid JSON") from exc


# ------------------------------------------------------------------ lead list
@router.get("")
async def list_leads(
    request: Request,
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=500),
    search: str = Query(default="", max_length=200),
    filters: str | None = Query(default=None, max_length=20000),
    sort: str = Query(default="", max_length=200),
    include_archived: bool = Query(default=False),
    view_id: uuid.UUID | None = Query(default=None),
    assigned_to_me: bool = Query(default=False),
):
    filter_spec = _parse_filters(filters)
    if view_id is not None:
        saved = await session.get(SavedView, view_id)
        if saved is None:
            raise NotFoundError("Saved view not found")
        filter_spec = saved.filters
    # Phase 11: organization + visibility scope (backend-enforced)
    from app.services import authorization as authz

    ctx = await _ctx_for(session, _user)
    extra = authz.visibility_clause(Lead, ctx)
    if assigned_to_me:
        mine = Lead.assigned_user_id == _user.id
        extra = mine if extra is None else extra.__and__(mine)
    rows, total = await workspace.search(
        session,
        page=page,
        page_size=min(page_size, 500),
        search=search or None,
        filters=filter_spec,
        sort=sort or None,
        include_archived=include_archived,
        extra_filter=extra,
    )
    return _page_envelope([lead.to_public_dict() for lead in rows], total, page, page_size)


# ------------------------------------------------------------------- quality
@router.get("/quality")
async def quality_overview(
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
):
    stats = await workspace.quality_stats(session)
    return {"success": True, "data": stats}


@router.post("/quality/recompute")
async def quality_recompute(
    session: DbSession,
    user=Depends(require_permission("leads.manage_quality")),
):
    updated = await workspace.recompute_quality(session)
    return {"success": True, "data": {"recomputed": updated}}


# ---------------------------------------------------------------------- tags
@router.get("/tags")
async def list_tags(
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
):
    return {"success": True, "data": await tag_service.list(session)}


@router.post("/tags")
async def create_tag(
    payload: TagCreate,
    session: DbSession,
    user=Depends(require_permission("leads.manage_tags")),
):
    tag = await tag_service.create(session, payload.name, color=payload.color, created_by=user.id)
    return {"success": True, "data": tag.to_public_dict()}


@router.patch("/tags/{tag_id}")
async def update_tag(
    tag_id: uuid.UUID,
    payload: TagUpdate,
    session: DbSession,
    user=Depends(require_permission("leads.manage_tags")),
):
    tag = await session.get(LeadTag, tag_id)
    if tag is None:
        raise NotFoundError("Tag not found")
    if payload.name:
        tag = await tag_service.rename(session, tag_id, payload.name)
    if payload.color is not None:
        tag.color = payload.color
        await session.commit()
        await session.refresh(tag)
    return {"success": True, "data": tag.to_public_dict()}


@router.delete("/tags/{tag_id}")
async def delete_tag(
    tag_id: uuid.UUID,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("leads.manage_tags")),
):
    tag = await session.get(LeadTag, tag_id)
    if tag is None:
        raise NotFoundError("Tag not found")
    name = tag.name
    await tag_service.delete(session, tag_id)
    await audit.log(session, action="lead_tag.deleted", resource_type="lead_tag",
                    resource_id=str(tag_id), actor_user_id=user.id, metadata={"name": name})
    return {"success": True, "data": {"deleted": True}}


# --------------------------------------------------------------------- views
@router.get("/views")
async def list_views(
    session: DbSession,
    user=Depends(require_permission("leads.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    rows, total = await view_service.list_visible(session, user_id=user.id, page=page, page_size=page_size)
    return _page_envelope([v.to_public_dict() for v in rows], total, page, page_size)


@router.post("/views")
async def create_view(
    payload: SavedViewCreate,
    session: DbSession,
    user=Depends(require_permission("leads.manage_views")),
):
    view = await view_service.create(
        session, name=payload.name, filters=payload.filters,
        visibility=payload.visibility, owner_id=user.id,
    )
    return {"success": True, "data": view.to_public_dict()}


@router.patch("/views/{view_id}")
async def update_view(
    view_id: uuid.UUID,
    payload: SavedViewUpdate,
    session: DbSession,
    user=Depends(require_permission("leads.manage_views")),
):
    if payload.name:
        await view_service.rename(session, view_id, name=payload.name, user_id=user.id, can_manage_all=False)
    if payload.filters is not None:
        await view_service.update_filters(session, view_id, filters=payload.filters, user_id=user.id, can_manage_all=False)
    view = await view_service.get_visible(session, view_id, user_id=user.id)
    return {"success": True, "data": view.to_public_dict()}


@router.delete("/views/{view_id}")
async def delete_view(
    view_id: uuid.UUID,
    session: DbSession,
    user=Depends(require_permission("leads.manage_views")),
):
    await view_service.delete(session, view_id, user_id=user.id, can_manage_all=False)
    return {"success": True, "data": {"deleted": True}}


# ---------------------------------------------------------------- duplicates
@router.get("/duplicates")
async def list_duplicates(
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
    status: str = Query(default="PENDING", max_length=20),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
):
    rows, total = await duplicates_service.list_candidates(
        session, status=status or None, page=page, page_size=page_size
    )
    items = []
    for candidate, lead_a, lead_b in rows:
        items.append(
            {
                **candidate.to_public_dict(),
                "lead_a": lead_a.to_public_dict(),
                "lead_b": lead_b.to_public_dict(),
            }
        )
    return _page_envelope(items, total, page, page_size)


@router.post("/duplicates/scan")
async def scan_duplicates(
    session: DbSession,
    user=Depends(require_permission("leads.manage_quality")),
):
    created = await duplicates_service.scan(session, origin="scan")
    return {"success": True, "data": {"candidates_created": created}}


@router.post("/duplicates/{candidate_id}/merge")
async def merge_duplicate(
    candidate_id: uuid.UUID,
    payload: MergeRequest,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("leads.merge")),
):
    try:
        primary_id = uuid.UUID(payload.primary_lead_id)
    except ValueError as exc:
        raise ValidationError("primary_lead_id must be a UUID") from exc
    candidate = await session.get(LeadDuplicateCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError("Duplicate candidate not found")
    if primary_id == candidate.lead_a_id:
        merged_id = candidate.lead_b_id
    elif primary_id == candidate.lead_b_id:
        merged_id = candidate.lead_a_id
    else:
        raise ValidationError("primary_lead_id is not part of this candidate pair")
    lead = await merge_service.merge(
        session, primary_id=primary_id, merged_id=merged_id,
        user_id=user.id, candidate_id=candidate.id,
    )
    await audit.log(
        session, action="lead.merged", resource_type="lead", resource_id=str(primary_id),
        actor_user_id=user.id,
        metadata={"merged_lead_id": str(merged_id), "candidate_id": str(candidate_id)},
    )
    return {"success": True, "data": lead.to_public_dict()}


@router.post("/duplicates/{candidate_id}/resolve")
async def resolve_duplicate(
    candidate_id: uuid.UUID,
    payload: DuplicateResolveRequest,
    session: DbSession,
    user=Depends(require_permission("leads.merge")),
):
    candidate = await session.get(LeadDuplicateCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError("Duplicate candidate not found")
    candidate.status = (
        DuplicateStatus.KEPT_BOTH.value
        if payload.action == "keep_both"
        else DuplicateStatus.IGNORED.value
    )
    candidate.resolved_at = datetime.now(timezone.utc)
    candidate.resolved_by = user.id
    candidate.resolution_note = payload.note
    await session.commit()
    return {"success": True, "data": candidate.to_public_dict()}


# ------------------------------------------------------------------- imports
@router.get("/imports")
async def list_imports(
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
):
    total = int(await session.scalar(select(func.count()).select_from(ImportBatch)) or 0)
    rows = (
        await session.scalars(
            select(ImportBatch).order_by(ImportBatch.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return _page_envelope([b.to_public_dict() for b in rows], total, page, page_size)


@router.get("/imports/{batch_id}")
async def get_import(
    batch_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
):
    batch = await session.get(ImportBatch, batch_id)
    if batch is None:
        raise NotFoundError("Import batch not found")
    return {"success": True, "data": batch.to_public_dict()}


@router.post("/import")
async def start_import(
    request: Request,
    session: DbSession,
    files: FileServiceDep,
    user=Depends(require_permission("leads.import")),
    file: UploadFile = File(...),
    format_name: str = Form(default=""),
):
    settings = _settings(request)
    if not file.filename:
        raise ValidationError("A file is required")
    lower = file.filename.lower()
    detected = (format_name or "").lower() or next(
        (ext for ext in ("xlsx", "csv", "jsonl", "json") if lower.endswith("." + ext)), ""
    )
    if detected not in ("csv", "xlsx", "json", "jsonl"):
        raise ValidationError("Unsupported import format (use CSV, XLSX, JSON or JSONL)")
    record = await files.store(
        session,
        content=file.file,
        filename=file.filename,
        mime_type=file.content_type,
        category="IMPORT",
        created_by=user.id,
        organization_id=(await _ctx_for(session, user)).organization_id,
        max_bytes=settings.QBIT_MAX_UPLOAD_MB * 1024 * 1024,
    )
    service = LeadImportService(request.app.state.storage, files)
    batch = await service.create_batch(
        session, file_record=record, format_name=detected,
        filename=file.filename, created_by=user.id,
    )
    return {"success": True, "data": batch.to_public_dict()}


@router.post("/imports/{batch_id}/mapping")
async def map_import(
    batch_id: uuid.UUID,
    payload: ImportMappingRequest,
    request: Request,
    session: DbSession,
    files: FileServiceDep,
    user=Depends(require_permission("leads.import")),
):
    service = LeadImportService(request.app.state.storage, files)
    batch = await session.get(ImportBatch, batch_id)
    if batch is None:
        raise NotFoundError("Import batch not found")
    batch.mapping = service._validate_mapping(payload.mapping)
    batch.options = {
        **(batch.options or {}),
        "duplicate_strategy": payload.duplicate_strategy,
        "default_status": payload.default_status,
        "tags": payload.tags or [],
        "source_name": payload.source_name,
    }
    await session.commit()

    settings = _settings(request)
    inspection = await service.inspect(session, batch)
    total = int(inspection.get("total_rows") or 0)
    queued = total > settings.QBIT_LEADS_INLINE_IMPORT_MAX_ROWS
    if not queued:
        batch = await service.run(session, batch)
    else:
        logger.info(
            "Import batch queued for worker",
            extra={"extra_fields": {"batch_id": str(batch.id), "rows": total}},
        )
    return {"success": True, "data": {**batch.to_public_dict(), "queued": queued}}


@router.get("/imports/{batch_id}/rejected")
async def download_rejected(
    batch_id: uuid.UUID,
    session: DbSession,
    files: FileServiceDep,
    _user=Depends(require_permission("leads.export")),
):
    batch = await session.get(ImportBatch, batch_id)
    if batch is None or batch.error_file_id is None:
        raise NotFoundError("No rejected-rows report for this import")
    file_record, stream = await files.open_download(session, batch.error_file_id)
    data = stream.read()
    stream.close()
    return Response(
        content=data,
        media_type=file_record.mime_type or "text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{file_record.name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# ------------------------------------------------------------------- exports
@router.get("/exports")
async def list_exports(
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
):
    total = int(await session.scalar(select(func.count()).select_from(LeadExportRecord)) or 0)
    rows = (
        await session.scalars(
            select(LeadExportRecord).order_by(LeadExportRecord.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return _page_envelope([r.to_public_dict() for r in rows], total, page, page_size)


@router.post("/export")
async def create_export(
    payload: ExportRequest,
    request: Request,
    session: DbSession,
    files: FileServiceDep,
    audit: AuditDep,
    user=Depends(require_permission("leads.export")),
):
    service = LeadExportService(request.app.state.storage, files)
    ids = None
    if payload.ids:
        try:
            ids = [uuid.UUID(x) for x in payload.ids[:10000]]
        except ValueError as exc:
            raise ValidationError("ids must be UUIDs") from exc
    lead_id = None
    if payload.lead_id:
        try:
            lead_id = uuid.UUID(payload.lead_id)
        except ValueError as exc:
            raise ValidationError("lead_id must be a UUID") from exc
    record = await service.create(
        session,
        format_name=payload.format,
        scope=payload.scope,
        filters=payload.filters,
        search=payload.search,
        sort=payload.sort,
        ids=ids,
        lead_id=lead_id,
        fields=payload.fields,
        page=payload.page,
        page_size=payload.page_size,
        created_by=user.id,
        organization_id=(await _ctx_for(session, user)).organization_id,
    )
    settings = _settings(request)
    estimated = await service.count(session, record)
    if estimated <= settings.QBIT_LEADS_INLINE_EXPORT_MAX_ROWS:
        record = await service.run(session, record)
        await audit.log(
            session, action="lead.export", resource_type="lead_export",
            resource_id=str(record.id), actor_user_id=user.id,
            metadata={"rows": record.row_count, "format": record.format},
        )
    else:
        logger.info(
            "Export queued for worker",
            extra={"extra_fields": {"export_id": str(record.id), "rows": estimated}},
        )
    return {"success": True, "data": record.to_public_dict()}


@router.get("/exports/{export_id}/download")
async def download_export(
    export_id: uuid.UUID,
    session: DbSession,
    files: FileServiceDep,
    _user=Depends(require_permission("leads.export")),
):
    record = await session.get(LeadExportRecord, export_id)
    if record is None or record.file_id is None:
        raise NotFoundError("Export not found")
    file_record, stream = await files.open_download(session, record.file_id)
    data = stream.read()
    stream.close()
    return Response(
        content=data,
        media_type=file_record.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{file_record.name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------- bulk
@router.post("/bulk")
async def bulk_action(
    payload: BulkActionRequest,
    request: Request,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("leads.view")),
):
    settings = _settings(request)
    if len(payload.lead_ids) > settings.QBIT_LEADS_MAX_BULK_IDS:
        raise ValidationError(f"Bulk actions are capped at {settings.QBIT_LEADS_MAX_BULK_IDS} ids")
    try:
        lead_ids = [uuid.UUID(x) for x in payload.lead_ids]
    except ValueError as exc:
        raise ValidationError("lead_ids must be UUIDs") from exc

    action = payload.action
    params = payload.params or {}
    # per-action permission — frontend visibility is NOT security (§32)
    required = {
        "set_status": "leads.edit",
        "add_tag": "leads.edit",
        "remove_tag": "leads.edit",
        "archive": "leads.archive",
        "restore": "leads.archive",
        "delete": "leads.archive",
        "export": "leads.export",
    }.get(action, "leads.edit")
    perms = await rbac_service.load_user_permissions(session, user.id)
    if required not in perms:
        raise PermissionDeniedError(f"Missing required permission: {required}")
    hard_allowed = False
    if action == "delete" and params.get("hard") is True:
        if "leads.delete" not in perms:
            raise PermissionDeniedError("Missing required permission: leads.delete")
        hard_allowed = True

    counts = await workspace.bulk_action(
        session, action=action, lead_ids=lead_ids, user_id=user.id,
        params=params, hard_delete_allowed=hard_allowed,
    )
    await audit.log(
        session, action=f"lead.bulk_{action}", resource_type="lead",
        resource_id=",".join(str(x)[:8] for x in lead_ids[:10]),
        actor_user_id=user.id, metadata={**counts, "params_keys": sorted(params.keys())},
    )
    return {"success": True, "data": counts}


# -------------------------------------------------------------- single leads
@router.post("")
async def create_lead(
    payload: LeadCreate,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("leads.create")),
):
    data = payload.model_dump(exclude_none=True)
    tags = data.pop("tags", None)
    lead = await workspace.create_lead(session, data, user_id=user.id, tags=tags)
    await audit.log(
        session, action="lead.created", resource_type="lead", resource_id=str(lead.id),
        actor_user_id=user.id, metadata={"source": "manual"},
    )
    return {"success": True, "data": lead.to_public_dict()}


@router.get("/{lead_id}")
async def get_lead(
    lead_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
):
    lead = await _visible_lead(session, lead_id, _user)
    notes, _n = await workspace.list_notes(session, lead_id)
    data = lead.to_public_dict()
    data["notes"] = [note.to_public_dict() for note in notes]
    return {"success": True, "data": data}


@router.patch("/{lead_id}")
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdate,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("leads.edit")),
):
    lead = await _visible_lead(session, lead_id, user)
    if lead.merged_into_id is not None:
        raise ValidationError("This lead has been merged and is read-only")
    data = payload.model_dump(exclude_none=True)
    status = data.pop("status", None)
    lead = await workspace.apply_update(session, lead, data, user_id=user.id)
    if status and status != lead.status:
        lead = await workspace.set_status(session, lead, status, user_id=user.id, commit=False)
    await session.commit()
    await audit.log(
        session, action="lead.updated", resource_type="lead", resource_id=str(lead.id),
        actor_user_id=user.id,
    )
    return {"success": True, "data": lead.to_public_dict()}


@router.post("/{lead_id}/status")
async def change_status(
    lead_id: uuid.UUID,
    payload: StatusChangeRequest,
    session: DbSession,
    user=Depends(require_permission("leads.edit")),
):
    lead = await _visible_lead(session, lead_id, user)
    lead = await workspace.set_status(session, lead, payload.status, user_id=user.id)
    return {"success": True, "data": lead.to_public_dict()}


@router.post("/{lead_id}/archive")
async def archive_lead(
    lead_id: uuid.UUID,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("leads.archive")),
):
    lead = await _visible_lead(session, lead_id, user)
    lead = await workspace.archive(session, lead, user_id=user.id)
    await audit.log(session, action="lead.archived", resource_type="lead",
                    resource_id=str(lead.id), actor_user_id=user.id)
    return {"success": True, "data": lead.to_public_dict()}


@router.post("/{lead_id}/restore")
async def restore_lead(
    lead_id: uuid.UUID,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("leads.archive")),
):
    lead = await _visible_lead(session, lead_id, user)
    lead = await workspace.restore(session, lead, user_id=user.id)
    await audit.log(session, action="lead.restored", resource_type="lead",
                    resource_id=str(lead.id), actor_user_id=user.id)
    return {"success": True, "data": lead.to_public_dict()}


@router.post("/{lead_id}/tags")
async def assign_tags(
    lead_id: uuid.UUID,
    payload: LeadTagAssignRequest,
    session: DbSession,
    user=Depends(require_permission("leads.edit")),
):
    lead = await _visible_lead(session, lead_id, user)
    for name in payload.tags:
        tag, _created = await tag_service.assign(session, lead.id, name, user_id=user.id, commit=False)
        await activities.log(
            session, lead.id, "tag_added", message=f"Tag '{tag.name}' added", user_id=user.id
        )
    await session.commit()
    lead = await _visible_lead(session, lead_id, user)
    return {"success": True, "data": lead.to_public_dict()}


@router.delete("/{lead_id}/tags/{tag_id}")
async def remove_tag(
    lead_id: uuid.UUID,
    tag_id: uuid.UUID,
    session: DbSession,
    user=Depends(require_permission("leads.edit")),
):
    lead = await _visible_lead(session, lead_id, user)
    tag = await session.get(LeadTag, tag_id)
    if tag is None:
        raise NotFoundError("Tag not found")
    removed = await tag_service.unassign(session, lead.id, tag_id, commit=False)
    if removed:
        await activities.log(
            session, lead.id, "tag_removed", message=f"Tag '{tag.name}' removed", user_id=user.id
        )
    await session.commit()
    lead = await _visible_lead(session, lead_id, user)
    return {"success": True, "data": lead.to_public_dict()}


@router.post("/{lead_id}/notes")
async def add_note(
    lead_id: uuid.UUID,
    payload: NoteCreate,
    session: DbSession,
    user=Depends(require_permission("leads.edit")),
):
    note = await workspace.add_note(session, lead_id, payload.content, user_id=user.id)
    return {"success": True, "data": note.to_public_dict()}


@router.get("/{lead_id}/activity")
async def lead_activity(
    lead_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    await _visible_lead(session, lead_id, _user)  # 404 when missing
    rows, total = await activities.list_for_lead(session, lead_id, page=page, page_size=page_size)
    return _page_envelope([a.to_public_dict() for a in rows], total, page, page_size)


# ------------------------------------------------- Phase 11: lead assignment


@router.get("/{lead_id}/assignment-history")
async def lead_assignment_history(
    lead_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("leads.view")),
):
    await _visible_lead(session, lead_id, _user)  # 404 when missing/invisible
    from app.models.enterprise import LeadAssignmentHistory

    rows = (
        await session.execute(
            select(LeadAssignmentHistory)
            .where(LeadAssignmentHistory.lead_id == lead_id)
            .order_by(LeadAssignmentHistory.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    return {"success": True, "data": [h.to_public_dict() for h in rows]}


@router.post("/{lead_id}/assignment")
async def assign_lead(
    lead_id: uuid.UUID,
    payload: dict,
    request: Request,
    session: DbSession,
    user=Depends(require_permission("leads.assign")),
):
    """Assign/reassign/unassign a lead to a user and/or team (history-kept)."""
    from app.models.enterprise import LeadAssignmentHistory
    from app.services import notifications as notification_service

    lead = await _visible_lead(session, lead_id, user)
    ctx = await _ctx_for(session, user)

    raw_user = payload.get("assigned_user_id")
    raw_team = payload.get("assigned_team_id")
    reason = payload.get("reason")
    new_user_id = uuid.UUID(raw_user) if raw_user else None
    new_team_id = uuid.UUID(raw_team) if raw_team else None

    if new_user_id is not None:
        from app.services import authorization as _authz

        target = await session.get(User, new_user_id)
        if target is None or await _authz.get_membership(
            session, new_user_id, ctx.organization_id
        ) is None:
            raise NotFoundError("Target user not found")
    if new_team_id is not None:
        from app.models.enterprise import Team

        team = await session.get(Team, new_team_id)
        if team is None or team.organization_id != ctx.organization_id:
            raise NotFoundError("Target team not found")

    prev_user, prev_team = lead.assigned_user_id, lead.assigned_team_id
    lead.assigned_user_id = new_user_id
    lead.assigned_team_id = new_team_id
    session.add(
        LeadAssignmentHistory(
            lead_id=lead.id,
            organization_id=lead.organization_id or ctx.organization_id,
            previous_user_id=prev_user,
            previous_team_id=prev_team,
            assigned_user_id=new_user_id,
            assigned_team_id=new_team_id,
            changed_by=user.id,
            reason=reason,
        )
    )
    await session.commit()

    if new_user_id is not None and new_user_id != user.id:
        await notification_service.emit(
            session,
            user_id=new_user_id,
            organization_id=ctx.organization_id,
            type="ASSIGNMENT",
            title="A lead was assigned to you",
            resource_type="lead",
            resource_id=str(lead.id),
        )
    await request.app.state.audit.log(
        session,
        action="lead.assigned",
        actor_user_id=user.id,
        resource_type="lead",
        resource_id=str(lead.id),
        ip_address=None,
        metadata={
            "previous_user_id": str(prev_user) if prev_user else None,
            "assigned_user_id": str(new_user_id) if new_user_id else None,
            "assigned_team_id": str(new_team_id) if new_team_id else None,
            "reason": reason,
        },
    )
    return {"success": True, "data": lead.to_public_dict()}


@router.post("/bulk-assignment")
async def bulk_assign_leads(
    payload: dict,
    request: Request,
    session: DbSession,
    user=Depends(require_permission("leads.assign")),
):
    """Idempotent bulk assignment (max 5000 ids; same-target = no-op)."""
    from app.models.enterprise import LeadAssignmentHistory

    ctx = await _ctx_for(session, user)
    raw_ids = payload.get("ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValidationError("ids must be a non-empty list")
    raw_user = payload.get("assigned_user_id")
    raw_team = payload.get("assigned_team_id")
    new_user_id = uuid.UUID(raw_user) if raw_user else None
    new_team_id = uuid.UUID(raw_team) if raw_team else None
    reason = payload.get("reason") or "BULK_ASSIGNMENT"

    updated = skipped = 0
    for raw_id in raw_ids[:5000]:
        try:
            lid = uuid.UUID(str(raw_id))
        except (ValueError, AttributeError):
            skipped += 1
            continue
        lead = await session.get(Lead, lid)
        if lead is None:
            skipped += 1
            continue
        if lead.organization_id is not None and lead.organization_id != ctx.organization_id:
            skipped += 1  # cross-tenant id: silently skip, never leak
            continue
        if lead.assigned_user_id == new_user_id and lead.assigned_team_id == new_team_id:
            skipped += 1
            continue
        session.add(
            LeadAssignmentHistory(
                lead_id=lead.id,
                organization_id=lead.organization_id or ctx.organization_id,
                previous_user_id=lead.assigned_user_id,
                previous_team_id=lead.assigned_team_id,
                assigned_user_id=new_user_id,
                assigned_team_id=new_team_id,
                changed_by=user.id,
                reason=reason,
            )
        )
        lead.assigned_user_id = new_user_id
        lead.assigned_team_id = new_team_id
        updated += 1
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="lead.bulk_assigned",
        actor_user_id=user.id,
        resource_type="lead",
        resource_id=None,
        metadata={"updated": updated, "skipped": skipped},
    )
    return {"success": True, "data": {"updated": updated, "skipped": skipped}}
