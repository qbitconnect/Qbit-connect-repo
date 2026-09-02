"""Phase 4 lead workspace UI — server-rendered pages over the same services.

    GET  /leads                     data workspace (search/filters/sort/bulk)
    GET  /leads/{id}                lead detail (+ edit/status/tags/notes forms)
    GET  /leads/import              wizard: upload
    GET  /leads/import/{batch}      wizard: mapping + options + results
    GET  /leads/imports             import history
    GET  /leads/duplicates          duplicate review (A vs B)
    GET  /leads/quality             data quality dashboard
    GET  /leads/exports             export history + download
    POST endpoints mirror the API permissions via require_ui_permission.

The UI is a thin client: every action calls the service layer; no lead logic
lives in templates; all numbers come from backend queries (§36).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.lead import (
    DuplicateStatus,
    ImportBatch,
    ImportStatus,
    LeadDuplicateCandidate,
    LeadExportRecord,
    LeadStatus,
    LeadTag,
    LeadTagAssignment,
    SavedView,
)
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
from app.ui import (
    _ctx,
    require_ui_permission,
    templates,
    ui_user_for,
)

leads_view = ui_user_for("leads.view")
require_leads_edit = require_ui_permission("leads.edit")
require_leads_archive = require_ui_permission("leads.archive")
require_leads_import = require_ui_permission("leads.import")
require_leads_export = require_ui_permission("leads.export")
require_leads_merge = require_ui_permission("leads.merge")
require_leads_manage_tags = require_ui_permission("leads.manage_tags")
require_leads_manage_quality = require_ui_permission("leads.manage_quality")
require_leads_manage_views = require_ui_permission("leads.manage_views")

router = APIRouter(tags=["leads-ui"])

workspace = LeadWorkspaceService()
tag_service = TagService()
view_service = SavedViewService()
duplicates_service = DuplicateDetectionService()
merge_service = MergeService()
activities = LeadActivityService()

DEFAULT_COLUMNS = (
    "business_name", "contact_name", "phone", "email", "website",
    "city", "state", "source", "status", "quality_score", "tags", "updated_at",
)
COLUMN_LABELS = {
    "business_name": "Business", "contact_name": "Contact", "first_name": "First",
    "last_name": "Last", "phone": "Phone", "email": "Email", "website": "Website",
    "city": "City", "state": "State", "country": "Country", "category": "Category",
    "industry": "Industry", "source": "Source", "source_type": "Source Type",
    "status": "Status", "quality_score": "Quality", "tags": "Tags",
    "created_at": "Created", "updated_at": "Updated", "scraped_at": "Scraped",
    "postal_code": "Postal Code",
}


def _perms(request: Request) -> set[str]:
    return getattr(request.state, "ui_permissions", None) or set()


def _filters_from_query(
    *, status: str, city: str, state: str, country: str, tag: str,
    has_email: str, has_phone: str, has_website: str,
    source: str, min_quality: int, advanced: str,
) -> tuple[dict | None, str | None]:
    """Build a validated filter group from simple UI controls + optional
    advanced JSON. Returns (spec, error)."""
    conditions: list[dict] = []
    if status:
        conditions.append({"field": "status", "op": "eq", "value": status.upper()})
    for field, value in (("city", city), ("state", state), ("country", country), ("source", source)):
        if value:
            conditions.append({"field": field, "op": "contains", "value": value})
    if tag:
        conditions.append({"field": "tag", "op": "eq", "value": tag})
    for field, value in (("has_email", has_email), ("has_phone", has_phone), ("has_website", has_website)):
        if value in ("1", "true", "True", "on"):
            conditions.append({"field": field, "op": "eq", "value": True})
        elif value in ("0", "false", "False", "off"):
            conditions.append({"field": field, "op": "eq", "value": False})
    if min_quality > 0:
        conditions.append({"field": "quality_score", "op": "gte", "value": min_quality})
    advanced_error = None
    if advanced.strip():
        try:
            advanced_spec = json.loads(advanced)
        except ValueError:
            advanced_error = "Advanced filters: invalid JSON"
            advanced_spec = None
        if advanced_spec is not None:
            try:
                from app.services.leads import filters as filter_engine

                filter_engine.build_filter_condition(advanced_spec)  # validation
                conditions.append(advanced_spec)
            except Exception as exc:  # user input — show, never crash
                advanced_error = f"Advanced filters: {exc}"
    if not conditions:
        return None, advanced_error
    return {"and": conditions}, advanced_error


# =================================================================== workspace
@router.get("/leads", response_class=HTMLResponse)
async def leads_home(
    request: Request,
    user: Annotated[object, Depends(leads_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    search: str = Query(default="", max_length=200),
    status: str = Query(default="", max_length=30),
    city: str = Query(default="", max_length=100),
    state: str = Query(default="", max_length=100),
    country: str = Query(default="", max_length=100),
    tag: str = Query(default="", max_length=100),
    has_email: str = Query(default="", max_length=5),
    has_phone: str = Query(default="", max_length=5),
    has_website: str = Query(default="", max_length=5),
    source: str = Query(default="", max_length=100),
    min_quality: int = Query(default=0, ge=0, le=100),
    advanced: str = Query(default="", max_length=20000),
    sort: str = Query(default="-created_at", max_length=200),
    cols: str = Query(default="", max_length=500),
    view_id: uuid.UUID | None = Query(default=None),
    include_archived: bool = Query(default=False),
    ok: str = Query(default=""),
    err: str = Query(default=""),
):
    filter_spec, advanced_error = _filters_from_query(
        status=status, city=city, state=state, country=country, tag=tag,
        has_email=has_email, has_phone=has_phone, has_website=has_website,
        source=source, min_quality=min_quality, advanced=advanced,
    )
    if view_id is not None:
        try:
            saved = await view_service.get_visible(session, view_id, user_id=user.id)
            filter_spec = saved.filters
        except Exception:  # noqa: BLE001 — unknown view just resets filters
            view_id = None

    page_size = 50
    rows, total = await workspace.search(
        session,
        page=page, page_size=page_size, search=search or None,
        filters=filter_spec, sort=sort,
        include_archived=include_archived or (status == "ARCHIVED"),
    )
    visible_cols = [c for c in (cols.split(",") if cols else DEFAULT_COLUMNS) if c in COLUMN_LABELS]
    from sqlalchemy import func

    from app.models.scrape import Lead

    async def _status_count(value: str) -> int:
        return int(await session.scalar(
            select(func.count()).select_from(Lead)
            .where(Lead.status == value, Lead.merged_into_id.is_(None))
        ) or 0)

    stats = {
        "total": await workspace.count_all(session),
        "new": await _status_count("NEW"),
        "verified": await _status_count("VERIFIED"),
        "qualified": await _status_count("QUALIFIED"),
        "duplicates": int(await session.scalar(
            select(func.count()).select_from(LeadDuplicateCandidate)
            .where(LeadDuplicateCandidate.status == DuplicateStatus.PENDING.value)
        ) or 0),
    }
    tags = await tag_service.list(session, with_counts=True)
    views, _vt = await view_service.list_visible(session, user_id=user.id)
    perms = _perms(request)

    query_string = "&".join(
        f"{k}={v}" for k, v in {
            "search": search, "status": status, "city": city, "state": state,
            "country": country, "tag": tag, "has_email": has_email,
            "has_phone": has_phone, "has_website": has_website, "source": source,
            "min_quality": min_quality or "", "advanced": advanced, "sort": sort,
            "cols": cols, "include_archived": "1" if include_archived else "",
        }.items() if v not in ("", None)
    )
    pages = max(1, -(-total // page_size))
    return templates.TemplateResponse(
        request, "leads/index.html",
        _ctx(
            request, user,
            leads=[lead.to_public_dict() for lead in rows],
            total=total, page=page, pages=pages, page_size=page_size,
            stats=stats, tags=tags, views=[v.to_public_dict() for v in views],
            visible_cols=visible_cols, column_labels=COLUMN_LABELS,
            all_columns=list(COLUMN_LABELS.keys()),
            f={"search": search, "status": status, "city": city, "state": state,
               "country": country, "tag": tag, "has_email": has_email,
               "has_phone": has_phone, "has_website": has_website,
               "source": source, "min_quality": min_quality,
               "advanced": advanced, "sort": sort, "cols": cols},
            view_id=view_id, query_string=query_string,
            statuses=[s.value for s in LeadStatus],
            can_edit="leads.edit" in perms,
            can_archive="leads.archive" in perms,
            can_delete="leads.delete" in perms,
            can_import="leads.import" in perms,
            can_export="leads.export" in perms,
            can_merge="leads.merge" in perms,
            can_manage_tags="leads.manage_tags" in perms,
            advanced_error=advanced_error,
            ok=ok, err=err,
        ),
    )


@router.post("/leads/bulk")
async def leads_bulk(
    request: Request,
    user: Annotated[object, Depends(require_leads_edit)],
    session: Annotated[AsyncSession, Depends(get_db)],
    action: str = Form(...),
    ids: list[str] = Form(default=[]),
    tag: str = Form(default=""),
    new_status: str = Form(default=""),
    confirm: str = Form(default=""),
    hard: str = Form(default=""),
    back: str = Form(default="/leads"),
):
    from app.core.errors import QBITError
    from app.services import rbac as rbac_service

    perms = await rbac_service.load_user_permissions(session, user.id)
    params: dict = {}
    if action == "add_tag":
        params = {"tags": [tag]} if tag else {}
        required = "leads.edit"
    elif action == "remove_tag":
        # by tag name via select
        tag_row = None
        if tag:
            from sqlalchemy import func as _f, select as _s
            from app.models.lead import LeadTag

            tag_row = await session.scalar(
                _s(LeadTag).where(_f.lower(LeadTag.name) == tag.strip().lower())
            )
        params = {"tag_ids": [tag_row.id]} if tag_row else {}
        required = "leads.edit"
    elif action in ("set_status",):
        params = {"status": new_status.upper()}
        required = "leads.edit"
    elif action == "delete" and hard == "1":
        params = {"hard": True, "confirm": confirm}
        required = "leads.delete"
    elif action == "delete":
        required = "leads.archive"
    else:
        required = "leads.edit"

    if required not in perms:
        return RedirectResponse(url="/403", status_code=303)
    try:
        lead_ids = [uuid.UUID(x) for x in ids]
    except ValueError:
        return RedirectResponse(url="/leads?err=Invalid+selection", status_code=303)
    try:
        hard_allowed = action == "delete" and hard == "1"
        counts = await workspace.bulk_action(
            session, action=action, lead_ids=lead_ids, user_id=user.id,
            params=params, hard_delete_allowed=hard_allowed,
        )
        return RedirectResponse(
            url=f"/leads?ok={counts['affected']}+leads+affected", status_code=303
        )
    except QBITError as exc:
        from urllib.parse import quote

        return RedirectResponse(url=f"/leads?err={quote(exc.message)}", status_code=303)


# ================================================================== lead detail
async def _load_lead(session: AsyncSession, lead_id: uuid.UUID):
    return await workspace.get(session, lead_id)


@router.post("/leads/{lead_id}/edit")
async def lead_edit(
    lead_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(require_leads_edit)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    from app.core.errors import QBITError

    form = await request.form()
    payload = {
        k: str(form.get(k, "")).strip()
        for k in (
            "business_name", "contact_name", "first_name", "last_name", "email",
            "phone", "website", "address", "city", "state", "postal_code",
            "country", "category", "industry",
        )
    }
    payload = {k: (v or None) for k, v in payload.items()}
    try:
        lead = await _load_lead(session, lead_id)
        await workspace.apply_update(session, lead, payload, user_id=user.id)
        return RedirectResponse(url=f"/leads/{lead_id}?ok=Saved", status_code=303)
    except QBITError as exc:
        from urllib.parse import quote

        return RedirectResponse(url=f"/leads/{lead_id}?err={quote(exc.message)}", status_code=303)


@router.post("/leads/{lead_id}/status")
async def lead_status(
    lead_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(require_leads_edit)],
    session: Annotated[AsyncSession, Depends(get_db)],
    status: str = Form(...),
):
    from app.core.errors import QBITError

    try:
        lead = await _load_lead(session, lead_id)
        await workspace.set_status(session, lead, status.strip().upper(), user_id=user.id)
        return RedirectResponse(url=f"/leads/{lead_id}?ok=Status+updated", status_code=303)
    except QBITError as exc:
        from urllib.parse import quote

        return RedirectResponse(url=f"/leads/{lead_id}?err={quote(exc.message)}", status_code=303)


@router.post("/leads/{lead_id}/archive")
async def lead_archive_ui(
    lead_id: uuid.UUID,
    user: Annotated[object, Depends(require_leads_archive)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    lead = await _load_lead(session, lead_id)
    await workspace.archive(session, lead, user_id=user.id)
    return RedirectResponse(url=f"/leads/{lead_id}?ok=Archived", status_code=303)


@router.post("/leads/{lead_id}/restore")
async def lead_restore_ui(
    lead_id: uuid.UUID,
    user: Annotated[object, Depends(require_leads_archive)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    lead = await _load_lead(session, lead_id)
    await workspace.restore(session, lead, user_id=user.id)
    return RedirectResponse(url=f"/leads/{lead_id}?ok=Restored", status_code=303)


@router.post("/leads/{lead_id}/tags")
async def lead_add_tag(
    lead_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(require_leads_edit)],
    session: Annotated[AsyncSession, Depends(get_db)],
    tag: str = Form(...),
):
    from app.core.errors import QBITError

    try:
        lead = await _load_lead(session, lead_id)
        created_tag, _ = await tag_service.assign(session, lead.id, tag.strip(), user_id=user.id)
        await activities.log(session, lead.id, "tag_added",
                             message=f"Tag '{created_tag.name}' added", user_id=user.id)
        await session.commit()
        return RedirectResponse(url=f"/leads/{lead_id}?ok=Tag+added", status_code=303)
    except QBITError as exc:
        from urllib.parse import quote

        return RedirectResponse(url=f"/leads/{lead_id}?err={quote(exc.message)}", status_code=303)


@router.post("/leads/{lead_id}/tags/{tag_id}/remove")
async def lead_remove_tag(
    lead_id: uuid.UUID,
    tag_id: uuid.UUID,
    user: Annotated[object, Depends(require_leads_edit)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    from app.models.lead import LeadTag

    lead = await _load_lead(session, lead_id)
    tag = await session.get(LeadTag, tag_id)
    if tag is not None:
        await tag_service.unassign(session, lead.id, tag_id, commit=False)
        await activities.log(session, lead.id, "tag_removed",
                             message=f"Tag '{tag.name}' removed", user_id=user.id)
        await session.commit()
    return RedirectResponse(url=f"/leads/{lead_id}?ok=Tag+removed", status_code=303)


@router.post("/leads/{lead_id}/notes")
async def lead_add_note(
    lead_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(require_leads_edit)],
    session: Annotated[AsyncSession, Depends(get_db)],
    content: str = Form(...),
):
    from app.core.errors import QBITError

    try:
        await workspace.add_note(session, lead_id, content, user_id=user.id)
        return RedirectResponse(url=f"/leads/{lead_id}?ok=Note+added", status_code=303)
    except QBITError as exc:
        from urllib.parse import quote

        return RedirectResponse(url=f"/leads/{lead_id}?err={quote(exc.message)}", status_code=303)


# =============================================================== import wizard
@router.get("/leads/import", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    user: Annotated[object, Depends(require_leads_import)],
    err: str = Query(default=""),
):
    return templates.TemplateResponse(
        request, "leads/import_upload.html", _ctx(request, user, err=err)
    )


@router.post("/leads/import")
async def import_upload_submit(
    request: Request,
    user: Annotated[object, Depends(require_leads_import)],
    session: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(None),
):
    from app.core.errors import QBITError

    storage = request.app.state.storage
    files = request.app.state.files
    service = LeadImportService(storage, files)
    settings = request.app.state.settings
    if file is None or not getattr(file, "filename", ""):
        return RedirectResponse(url="/leads/import?err=Choose+a+file", status_code=303)
    lower = file.filename.lower()
    detected = next(
        (ext for ext in ("xlsx", "csv", "jsonl", "json") if lower.endswith("." + ext)), ""
    )
    if not detected:
        return RedirectResponse(
            url="/leads/import?err=Unsupported+format+(CSV,+XLSX,+JSON,+JSONL)", status_code=303
        )
    try:
        record = await files.store(
            session, content=file.file, filename=file.filename,
            mime_type=file.content_type, category="IMPORT", created_by=user.id,
            max_bytes=settings.QBIT_MAX_UPLOAD_MB * 1024 * 1024,
        )
        batch = await service.create_batch(
            session, file_record=record, format_name=detected,
            filename=file.filename, created_by=user.id,
        )
    except QBITError as exc:
        from urllib.parse import quote

        return RedirectResponse(url=f"/leads/import?err={quote(exc.message)}", status_code=303)
    return RedirectResponse(url=f"/leads/import/{batch.id}", status_code=303)


@router.get("/leads/import/{batch_id}", response_class=HTMLResponse)
async def import_map(
    batch_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(require_leads_import)],
    session: Annotated[AsyncSession, Depends(get_db)],
    err: str = Query(default=""),
):
    from app.core.errors import QBITError

    service = LeadImportService(request.app.state.storage, request.app.state.files)
    batch = await session.get(ImportBatch, batch_id)
    if batch is None:
        return RedirectResponse(url="/leads/import", status_code=303)
    if batch.status == ImportStatus.QUEUED.value and not batch.mapping:
        try:
            inspection = await service.inspect(session, batch)
        except QBITError as exc:
            return templates.TemplateResponse(
                request, "leads/import_map.html",
                _ctx(request, user, batch=batch.to_public_dict(), inspection=None,
                     mapping={}, columns=[], mappable=(), err=exc.message),
            )
        return templates.TemplateResponse(
            request, "leads/import_map.html",
            _ctx(request, user, batch=batch.to_public_dict(), inspection=inspection,
                 mapping=batch.mapping or {}, columns=inspection.get("columns", []),
                 sample=inspection.get("sample", []),
                 mappable=service.MAPPABLE_FIELDS + service.SPECIAL_FIELDS,
                 statuses=[s.value for s in LeadStatus],
                 total_rows=inspection.get("total_rows", 0), err=err),
        )
    # results / progress view
    return templates.TemplateResponse(
        request, "leads/import_results.html",
        _ctx(request, user, batch=batch.to_public_dict(), err=err),
    )


@router.get("/leads/import/{batch_id}/status")
async def import_status(
    batch_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(require_leads_import)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    batch = await session.get(ImportBatch, batch_id)
    if batch is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(batch.to_public_dict())


@router.post("/leads/import/{batch_id}")
async def import_map_submit(
    batch_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(require_leads_import)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    from app.core.errors import QBITError
    from urllib.parse import quote

    service = LeadImportService(request.app.state.storage, request.app.state.files)
    batch = await session.get(ImportBatch, batch_id)
    if batch is None or batch.status != ImportStatus.QUEUED.value:
        return RedirectResponse(url=f"/leads/import/{batch_id}", status_code=303)
    form = await request.form()
    mapping = {}
    for key in form:
        if key.startswith("map__") and str(form[key]).strip():
            mapping[key.removeprefix("map__")] = str(form[key]).strip()
    options = {
        "duplicate_strategy": str(form.get("duplicate_strategy", "SKIP_DUPLICATES")),
        "default_status": str(form.get("default_status", "NEW")).upper(),
        "tags": [t.strip() for t in str(form.get("tags", "")).split(",") if t.strip()],
        "source_name": str(form.get("source_name", "")).strip() or None,
    }
    try:
        batch.mapping = service._validate_mapping(mapping)
        batch.options = {**(batch.options or {}), **options}
        await session.commit()
        settings = request.app.state.settings
        inspection = await service.inspect(session, batch)
        total = int(inspection.get("total_rows") or 0)
        if total > settings.QBIT_LEADS_INLINE_IMPORT_MAX_ROWS:
            # leave QUEUED — the worker's data-jobs loop picks it up (§39)
            pass
    except QBITError as exc:
        return RedirectResponse(url=f"/leads/import/{batch_id}?err={quote(exc.message)}", status_code=303)
    return RedirectResponse(url=f"/leads/import/{batch_id}", status_code=303)


@router.get("/leads/imports", response_class=HTMLResponse)
async def imports_history(
    request: Request,
    user: Annotated[object, Depends(leads_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
):
    from sqlalchemy import func, select

    total = int(await session.scalar(select(func.count()).select_from(ImportBatch)) or 0)
    rows = (
        await session.scalars(
            select(ImportBatch).order_by(ImportBatch.created_at.desc())
            .offset((page - 1) * 25).limit(25)
        )
    ).all()
    pages = max(1, -(-total // 25))
    return templates.TemplateResponse(
        request, "leads/imports.html",
        _ctx(request, user, batches=[b.to_public_dict() for b in rows],
             total=total, page=page, pages=pages),
    )


# ================================================================== duplicates
@router.get("/leads/duplicates", response_class=HTMLResponse)
async def duplicates_review(
    request: Request,
    user: Annotated[object, Depends(leads_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    status: str = Query(default="PENDING", max_length=20),
    ok: str = Query(default=""),
    err: str = Query(default=""),
):
    rows, total = await duplicates_service.list_candidates(
        session, status=status or None, page=page, page_size=20
    )
    items = [
        {"candidate": c.to_public_dict(), "a": a.to_public_dict(), "b": b.to_public_dict()}
        for c, a, b in rows
    ]
    perms = _perms(request)
    pages = max(1, -(-total // 20))
    return templates.TemplateResponse(
        request, "leads/duplicates.html",
        _ctx(request, user, items=items, total=total, page=page, pages=pages,
             status=status, statuses=[s.value for s in DuplicateStatus],
             can_merge="leads.merge" in perms, ok=ok, err=err),
    )


@router.post("/leads/duplicates/{candidate_id}/merge")
async def duplicates_merge_ui(
    candidate_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(require_leads_merge)],
    session: Annotated[AsyncSession, Depends(get_db)],
    primary: str = Form(...),
):
    from app.core.errors import QBITError
    from urllib.parse import quote

    try:
        candidate = await session.get(LeadDuplicateCandidate, candidate_id)
        if candidate is None:
            raise QBITError("Duplicate candidate not found")
        primary_id = uuid.UUID(primary)
        merged_id = (
            candidate.lead_b_id if primary_id == candidate.lead_a_id else candidate.lead_a_id
        )
        await merge_service.merge(
            session, primary_id=primary_id, merged_id=merged_id,
            user_id=user.id, candidate_id=candidate.id,
        )
        from app.services.audit import AuditService

        await AuditService().log(session, action="lead.merged", resource_type="lead",
                                 resource_id=str(primary_id), actor_user_id=user.id,
                                 metadata={"merged_lead_id": str(merged_id)})
        return RedirectResponse(url="/leads/duplicates?ok=Merged", status_code=303)
    except (QBITError, ValueError) as exc:
        return RedirectResponse(
            url=f"/leads/duplicates?err={quote(str(exc)[:200])}", status_code=303
        )


@router.post("/leads/duplicates/{candidate_id}/resolve")
async def duplicates_resolve_ui(
    candidate_id: uuid.UUID,
    user: Annotated[object, Depends(require_leads_merge)],
    session: Annotated[AsyncSession, Depends(get_db)],
    action: str = Form(...),
):
    candidate = await session.get(LeadDuplicateCandidate, candidate_id)
    if candidate is not None:
        candidate.status = (
            DuplicateStatus.KEPT_BOTH.value if action == "keep_both" else DuplicateStatus.IGNORED.value
        )
        candidate.resolved_at = datetime.now(timezone.utc)
        candidate.resolved_by = user.id
        await session.commit()
    return RedirectResponse(url="/leads/duplicates?ok=Resolved", status_code=303)


# ====================================================================== quality
@router.get("/leads/quality", response_class=HTMLResponse)
async def quality_dashboard(
    request: Request,
    user: Annotated[object, Depends(leads_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
    ok: str = Query(default=""),
    err: str = Query(default=""),
):
    stats = await workspace.quality_stats(session)
    perms = _perms(request)
    return templates.TemplateResponse(
        request, "leads/quality.html",
        _ctx(request, user, stats=stats,
             can_manage="leads.manage_quality" in perms, ok=ok, err=err),
    )


@router.post("/leads/quality/scan")
async def quality_scan(
    request: Request,
    user: Annotated[object, Depends(require_leads_manage_quality)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    created = await duplicates_service.scan(session, origin="scan")
    return RedirectResponse(url=f"/leads/quality?ok=Scan+complete:+{created}+candidates", status_code=303)


@router.post("/leads/quality/recompute")
async def quality_recompute_ui(
    request: Request,
    user: Annotated[object, Depends(require_leads_manage_quality)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    updated = await workspace.recompute_quality(session)
    return RedirectResponse(
        url=f"/leads/quality?ok={updated}+scores+recomputed", status_code=303
    )


# ====================================================================== exports
@router.get("/leads/exports", response_class=HTMLResponse)
async def exports_history(
    request: Request,
    user: Annotated[object, Depends(leads_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    ok: str = Query(default=""),
    err: str = Query(default=""),
):
    from sqlalchemy import func, select

    total = int(await session.scalar(select(func.count()).select_from(LeadExportRecord)) or 0)
    rows = (
        await session.scalars(
            select(LeadExportRecord).order_by(LeadExportRecord.created_at.desc())
            .offset((page - 1) * 25).limit(25)
        )
    ).all()
    tags = await tag_service.list(session, with_counts=False)
    can_export = "leads.export" in _perms(request)
    return templates.TemplateResponse(
        request, "leads/exports.html",
        _ctx(request, user, exports=[r.to_public_dict() for r in rows], total=total,
             page=page, pages=max(1, -(-total // 25)), tags=tags,
             can_export=can_export, ok=ok, err=err),
    )


# NOTE: the parameterized lead-detail route is deliberately registered AFTER
# every static /leads/* page (/import, /imports, /duplicates, /quality,
# /exports). FastAPI matches in registration order, so an earlier
# "/leads/{lead_id}" would shadow them (GET /leads/import -> 422).
@router.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail(
    lead_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(leads_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
    ok: str = Query(default=""),
    err: str = Query(default=""),
):
    try:
        lead = await _load_lead(session, lead_id)
    except Exception:
        return RedirectResponse(url="/leads", status_code=303)
    notes, notes_total = await workspace.list_notes(session, lead_id)
    activity, _at = await activities.list_for_lead(session, lead_id, page_size=100)
    tags = [
        {"id": str(row[0]), "name": row[1]}
        for row in (await session.execute(
            select(LeadTag.id, LeadTag.name)
            .join(LeadTagAssignment, LeadTagAssignment.tag_id == LeadTag.id)
            .where(LeadTagAssignment.lead_id == lead.id)
            .order_by(LeadTag.name)
        )).all()
    ]
    perms = _perms(request)
    return templates.TemplateResponse(
        request, "leads/detail.html",
        _ctx(
            request, user,
            lead=lead, lead_data=lead.to_public_dict(),
            tags=tags, notes=[n.to_public_dict() for n in notes],
            activity=[a.to_public_dict() for a in activity],
            metadata_json=json.dumps(lead.metadata_json or {}, indent=2, default=str)[:20000],
            statuses=[s.value for s in LeadStatus],
            can_edit="leads.edit" in perms,
            can_archive="leads.archive" in perms,
            can_export="leads.export" in perms,
            can_manage_tags="leads.manage_tags" in perms,
            ok=ok, err=err,
        ),
    )


@router.post("/leads/export")
async def export_submit(
    request: Request,
    user: Annotated[object, Depends(require_leads_export)],
    session: Annotated[AsyncSession, Depends(get_db)],
    format_name: str = Form(..., alias="format"),
    scope: str = Form(default="filtered"),
    fields_mode: str = Form(default="all"),
    fields: str = Form(default=""),
    search: str = Form(default=""),
    status: str = Form(default=""),
    city: str = Form(default=""),
    state: str = Form(default=""),
    tag: str = Form(default=""),
    has_email: str = Form(default=""),
    has_phone: str = Form(default=""),
    advanced: str = Form(default=""),
    ids: str = Form(default=""),
    lead_id: str = Form(default=""),
    back: str = Form(default="/leads"),
):
    from app.core.errors import QBITError
    from urllib.parse import quote

    service = LeadExportService(request.app.state.storage, request.app.state.files)
    filter_spec, _err = _filters_from_query(
        status=status, city=city, state=state, country="", tag=tag,
        has_email=has_email, has_phone=has_phone, has_website="",
        source="", min_quality=0, advanced=advanced,
    )
    selected_ids = None
    single_lead_id = None
    if scope == "selected":
        selected_ids = [x for x in (ids or "").split(",") if x.strip()]
    elif scope == "lead" and lead_id.strip():
        single_lead_id = lead_id.strip()
    try:
        field_list = None
        if fields_mode == "custom" and fields.strip():
            field_list = [f.strip() for f in fields.split(",") if f.strip()]
        record = await service.create(
            session, format_name=format_name, scope=scope,
            filters=filter_spec, search=search or None,
            ids=selected_ids, lead_id=single_lead_id, fields=field_list, created_by=user.id,
        )
        estimated = await service.count(session, record)
        if estimated <= request.app.state.settings.QBIT_LEADS_INLINE_EXPORT_MAX_ROWS:
            record = await service.run(session, record)
            from app.services.audit import AuditService

            await AuditService().log(
                session, action="lead.export", resource_type="lead_export",
                resource_id=str(record.id), actor_user_id=user.id,
                metadata={"rows": record.row_count, "format": record.format},
            )
            return RedirectResponse(url="/leads/exports?ok=Export+ready", status_code=303)
        return RedirectResponse(
            url="/leads/exports?ok=Export+queued+for+background+processing", status_code=303
        )
    except QBITError as exc:
        return RedirectResponse(url=f"/leads/exports?err={quote(exc.message)}", status_code=303)


@router.get("/leads/exports/{export_id}/download")
async def export_download_ui(
    export_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(require_leads_export)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    record = await session.get(LeadExportRecord, export_id)
    if record is None or record.file_id is None:
        return RedirectResponse(url="/leads/exports?err=Export+file+not+ready", status_code=303)
    files = request.app.state.files
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


# ================================================================== saved views
@router.post("/leads/views/save")
async def save_view_ui(
    request: Request,
    user: Annotated[object, Depends(require_leads_manage_views)],
    session: Annotated[AsyncSession, Depends(get_db)],
    name: str = Form(...),
    spec: str = Form(default=""),
):
    from app.core.errors import QBITError
    from urllib.parse import quote

    try:
        filter_spec = json.loads(spec) if spec.strip() else None
        await view_service.create(
            session, name=name, filters=filter_spec or {}, visibility="PRIVATE",
            owner_id=user.id,
        )
        return RedirectResponse(url="/leads?ok=View+saved", status_code=303)
    except (QBITError, ValueError) as exc:
        return RedirectResponse(url=f"/leads?err={quote(str(exc)[:200])}", status_code=303)


@router.post("/leads/views/{view_id}/delete")
async def delete_view_ui(
    view_id: uuid.UUID,
    user: Annotated[object, Depends(require_leads_manage_views)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    from app.core.errors import QBITError

    try:
        await view_service.delete(session, view_id, user_id=user.id, can_manage_all=False)
    except QBITError:
        pass
    return RedirectResponse(url="/leads?ok=View+deleted", status_code=303)
