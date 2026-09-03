"""Marketing template API (Phase 5 §29: /templates endpoints)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuditDep, DbSession, require_permission
from app.core.errors import NotFoundError, ValidationError
from app.models.scrape import Lead
from app.schemas.marketing import TemplateCreate, TemplatePreviewRequest, TemplateUpdate
from app.services.marketing import TemplateService

router = APIRouter(prefix="/templates", tags=["templates"])

templates_service = TemplateService()


def _page(items: list, total: int, page: int, page_size: int) -> dict:
    return {
        "success": True,
        "data": {
            "items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": max(1, -(-total // page_size)) if total else 1,
        },
    }


@router.get("")
async def list_templates(
    session: DbSession,
    _user=Depends(require_permission("templates.view")),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    channel: str | None = Query(default=None, max_length=20),
    status: str | None = Query(default=None, max_length=20),
):
    rows, total = await templates_service.list(
        session, channel=channel, status=status, page=page, page_size=page_size,
    )
    return _page([t.to_public_dict() for t in rows], total, page, page_size)


@router.post("", status_code=201)
async def create_template(
    payload: TemplateCreate,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("templates.create")),
):
    template = await templates_service.create(
        session, name=payload.name, channel=payload.channel,
        subject=payload.subject, body=payload.body,
        language=payload.language, status=payload.status,
        variables=payload.variables, created_by=user.id,
    )
    await audit.log(session, action="template.created", resource_type="campaign_template",
                    resource_id=str(template.id), actor_user_id=user.id,
                    metadata={"channel": template.channel})
    return {"success": True, "data": template.to_public_dict()}


@router.get("/{template_id}")
async def get_template(
    template_id: uuid.UUID,
    session: DbSession,
    _user=Depends(require_permission("templates.view")),
):
    template = await templates_service.get(session, template_id)
    return {"success": True, "data": template.to_public_dict()}


@router.patch("/{template_id}")
async def update_template(
    template_id: uuid.UUID,
    payload: TemplateUpdate,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("templates.edit")),
):
    template = await templates_service.update(
        session, template_id, name=payload.name, subject=payload.subject,
        body=payload.body, status=payload.status, language=payload.language,
        variables=payload.variables,
    )
    await audit.log(session, action="template.updated", resource_type="campaign_template",
                    resource_id=str(template.id), actor_user_id=user.id)
    return {"success": True, "data": template.to_public_dict()}


@router.delete("/{template_id}")
async def delete_template(
    template_id: uuid.UUID,
    session: DbSession,
    audit: AuditDep,
    user=Depends(require_permission("templates.delete")),
):
    """In-use templates are ARCHIVED (campaign reproducibility), unused are removed."""
    template = await templates_service.get(session, template_id)
    was_archived = template.status
    await templates_service.delete(session, template_id)
    await audit.log(session, action="template.deleted", resource_type="campaign_template",
                    resource_id=str(template_id), actor_user_id=user.id,
                    metadata={"previous_status": was_archived})
    return {"success": True, "data": {"deleted": True}}


@router.post("/{template_id}/preview")
async def preview_template(
    template_id: uuid.UUID,
    payload: TemplatePreviewRequest,
    session: DbSession,
    _user=Depends(require_permission("templates.view")),
):
    """Render a preview with a real sample lead or supplied sample values.

    Substitution is strictly allowlisted {{variable}} — never code execution.
    """
    template = await templates_service.get(session, template_id)
    if payload.lead_id:
        try:
            lead_uuid = uuid.UUID(payload.lead_id)
        except (ValueError, TypeError) as exc:
            raise ValidationError("lead_id must be a UUID") from exc
        lead = await session.get(Lead, lead_uuid)
        if lead is None:
            raise NotFoundError("Sample lead not found")
        preview = templates_service.preview(template, lead)
    else:
        from app.services.marketing.template import render

        sample = payload.sample or {}
        values = {k: (v if v is not None else "") for k, v in sample.items()}
        preview = {
            "subject": render(template.subject, values) if template.subject else None,
            "body": render(template.body, values),
        }
    return {"success": True, "data": preview}
