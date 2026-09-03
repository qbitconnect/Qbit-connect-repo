"""Marketing template endpoints (Phase 7 §10–§12, §37, §46)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import DbSession, get_client_ip, get_user_agent, require_permission
from app.core.errors import NotFoundError, ValidationError
from app.models.marketing import MarketingTemplate
from app.models.user import User
from app.schemas.marketing import ActionOut, ListOut, TemplateCreate, TemplatePreviewRequest, TemplateUpdate
from app.services.marketing import templates as template_engine

router = APIRouter(prefix="/templates", tags=["templates"])


def _permission_for(channel: str, action: str) -> str:
    prefix = "email" if channel == "EMAIL" else "whatsapp"
    return f"{prefix}.templates.{action}"


async def _guard(session, request: Request, actor: User, channel: str, action: str) -> None:
    """Backend-enforced channel permission (UI is never the boundary)."""
    from app.services import rbac as rbac_service

    cached = getattr(request.state, "permissions", None)
    if cached is None:
        cached = await rbac_service.load_user_permissions(session, actor.id)
        request.state.permissions = cached
    if _permission_for(channel, action) not in cached:
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError(f"Missing required permission: {_permission_for(channel, action)}")


@router.get("", response_model=ListOut)
async def list_templates(
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("marketing.view"))],
    channel: str | None = Query(default=None, pattern="^(EMAIL|WHATSAPP)$"),
    status: str | None = Query(default=None, pattern="^(DRAFT|ACTIVE|ARCHIVED)$"),
):
    from sqlalchemy import select

    query = select(MarketingTemplate).order_by(MarketingTemplate.updated_at.desc())
    if channel:
        query = query.where(MarketingTemplate.channel == channel)
    if status:
        query = query.where(MarketingTemplate.status == status)
    rows = (await session.scalars(query)).all()
    return ListOut(data={"items": [t.to_public_dict() for t in rows], "total": len(rows)})


@router.post("", response_model=ActionOut, status_code=201)
async def create_template(
    payload: TemplateCreate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("marketing.view"))],
):
    await _guard(session, request, actor, payload.channel, "manage")

    unknown = template_engine.validate_variable_usage(
        payload.subject, payload.html_body, payload.text_body, payload.body
    )
    declared_unknown = [v for v in payload.variables if v not in template_engine.ALLOWED_VARIABLES]
    if unknown or declared_unknown:
        raise ValidationError(
            "Unknown template variables (spec §11 whitelist)",
            details={"unknown": sorted(set(unknown + declared_unknown))},
        )
    if payload.channel == "EMAIL":
        if not (payload.html_body or payload.text_body):
            raise ValidationError("Email templates need an HTML or plain-text body")
        if payload.html_body:
            # sanitize on save (spec §38) — what you store is what gets sent
            payload.html_body = template_engine.sanitize_html(payload.html_body)

    used = template_engine.extract_variables(payload.subject, payload.html_body, payload.text_body, payload.body)
    template = MarketingTemplate(
        name=payload.name,
        channel=payload.channel,
        status="DRAFT",
        subject=payload.subject,
        html_body=payload.html_body,
        text_body=payload.text_body,
        body=payload.body,
        language=payload.language,
        category=payload.category,
        variables=sorted(set(used) | set(payload.variables)),
        metadata_json=payload.metadata,
        created_by=actor.id,
    )
    session.add(template)
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="template.created",
        actor_user_id=actor.id,
        resource_type="marketing_template",
        resource_id=str(template.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        metadata={"channel": payload.channel},
    )
    return ActionOut(data={"template": template.to_public_dict()})


@router.get("/{template_id}", response_model=ActionOut)
async def get_template(
    template_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("marketing.view"))],
):
    template = await session.get(MarketingTemplate, template_id)
    if template is None:
        raise NotFoundError("Template not found")
    return ActionOut(data={"template": template.to_public_dict()})


@router.patch("/{template_id}", response_model=ActionOut)
async def update_template(
    template_id: uuid.UUID,
    payload: TemplateUpdate,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("marketing.view"))],
):
    template = await session.get(MarketingTemplate, template_id)
    if template is None:
        raise NotFoundError("Template not found")
    await _guard(session, request, actor, template.channel, "manage")

    if payload.name is not None:
        template.name = payload.name.strip()
    if payload.status is not None:
        template.status = payload.status
    if payload.subject is not None:
        template.subject = payload.subject
    if payload.html_body is not None:
        template.html_body = template_engine.sanitize_html(payload.html_body)
    if payload.text_body is not None:
        template.text_body = payload.text_body
    if payload.body is not None:
        template.body = payload.body
    if payload.language is not None:
        template.language = payload.language
    if payload.category is not None:
        template.category = payload.category
    if payload.metadata is not None:
        template.metadata_json = payload.metadata
    if payload.variables is not None:
        bad = [v for v in payload.variables if v not in template_engine.ALLOWED_VARIABLES]
        if bad:
            raise ValidationError(f"Unknown template variables: {', '.join(bad)}")
        template.variables = sorted(set(template.variables or []) | set(payload.variables))

    unknown = template_engine.validate_variable_usage(
        template.subject, template.html_body, template.text_body, template.body
    )
    if unknown:
        raise ValidationError(
            f"Template uses unknown variables: {', '.join(unknown)}",
            details={"unknown": unknown},
        )
    await session.commit()
    await request.app.state.audit.log(
        session,
        action="template.modified",
        actor_user_id=actor.id,
        resource_type="marketing_template",
        resource_id=str(template.id),
    )
    return ActionOut(data={"template": template.to_public_dict()})


@router.delete("/{template_id}", response_model=ActionOut)
async def delete_template(
    template_id: uuid.UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("marketing.view"))],
):
    template = await session.get(MarketingTemplate, template_id)
    if template is None:
        raise NotFoundError("Template not found")
    await _guard(session, request, actor, template.channel, "manage")
    await session.delete(template)
    await session.commit()
    return ActionOut(data={"deleted": True})


@router.post("/{template_id}/preview", response_model=ActionOut)
async def preview_template(
    template_id: uuid.UUID,
    payload: TemplatePreviewRequest,
    request: Request,
    session: DbSession,
    actor: Annotated[User, Depends(require_permission("marketing.view"))],
):
    """Sample-lead render with honest warnings (spec §37)."""
    template = await session.get(MarketingTemplate, template_id)
    if template is None:
        raise NotFoundError("Template not found")

    sample = {
        "first_name": "Sagar",
        "last_name": "Patel",
        "business_name": "Example Company",
        "email": "sagar@example.com",
        "phone": "+91 90000 00000",
        "city": "Ahmedabad",
        "state": "Gujarat",
        "country": "India",
        "website": "https://example.com",
    }
    renderer = template_engine.TemplateRenderer(
        unsubscribe_url=f"{request.app.state.settings.QBIT_PUBLIC_BASE_URL}/unsubscribe/sample",
        company_name="QBIT Connect",
        company_address="—",
    )
    rendered = renderer.render(
        channel=template.channel,
        subject=template.subject,
        html_body=template.html_body,
        text_body=template.text_body,
        body=template.body,
        values={**sample, **(payload.values or {})},
        sanitize=False,  # preview shows user HTML; already sanitized at save
    )
    return ActionOut(
        data={
            "preview": {
                "subject": rendered.subject,
                "html": rendered.html,
                "text": rendered.text,
            },
            "warnings": rendered.warnings,
        }
    )
