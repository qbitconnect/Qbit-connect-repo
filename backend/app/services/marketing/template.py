"""Template system (Phase 5 §9, §10; Phase 7 §10–§12, §38, §39).

Safe-by-construction variable substitution:
- only {{variable_name}} placeholders are recognized — there is NO expression
  language, no filters, no loops: template content is NEVER executed as code
- substitution values are plain strings derived from lead fields; HTML/URL
  context is the caller's responsibility and values are never interpolated
  into attributes by the engine itself
- validation reports required-but-unknown, unknown, missing variables and
  channel rule violations (length, subject requirement)

Phase 7 (EMAIL):
- `body` holds the HTML body; it is SANITIZED (email-safe allowlist) before
  storage and again at render time (defense in depth, §38)
- `components.text` holds the optional plain-text fallback (§10)
- the variable allowlist extends with email/phone/website/unsubscribe_url/
  company fields (§11, §12) — still an allowlist, never arbitrary code
- subjects are CRLF-guarded (§39)
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.marketing import CampaignTemplate, TemplateStatus
from app.services.marketing.channels import get_channel
from app.services.marketing.email_compose import header_safe, sanitize_html

VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
#: anything wrapped in {{ }} that is NOT a clean identifier — potential
#: injection attempt — is detected and rejected at validation (§10)
BRACKET_RE = re.compile(r"\{\{(.*?)\}\}")
IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

#: lead fields allowed as substitution sources (allowlist — nothing arbitrary)
RENDER_VARIABLES: dict[str, str] = {
    "first_name": "first_name",
    "last_name": "last_name",
    "contact_name": "contact_name",
    "business_name": "business_name",
    "company_name": "business_name",
    "city": "city",
    "state": "state",
    "country": "country",
    "category": "category",
    "industry": "industry",
}

# --- Phase 7 §11/§12: email-specific render variables ------------------------
#: rendered from the lead row when present (never invented)
EMAIL_LEAD_VARIABLES: dict[str, str] = {
    "email": "email",
    "phone": "phone",
    "website": "website",
}
#: rendered from campaign/recipient context (per-send values)
EMAIL_CONTEXT_VARIABLES = {"unsubscribe_url", "company_address"}
#: the full EMAIL variable allowlist (§11 + §12)
EMAIL_RENDER_VARIABLES = {
    **RENDER_VARIABLES,
    **EMAIL_LEAD_VARIABLES,
    **{name: name for name in EMAIL_CONTEXT_VARIABLES},
}


def extract_variables(text: str) -> set[str]:
    """Variable names actually present in template text."""
    return set(VARIABLE_RE.findall(text or ""))


def malformed_blocks(text: str) -> list[str]:
    """{{...}} blocks whose content is not a plain identifier (injection or
    syntax errors). The renderer leaves them untouched, but validation must
    reject them — template content is data, never expression syntax."""
    bad = []
    for inner in BRACKET_RE.findall(text or ""):
        if not IDENTIFIER_RE.match(inner.strip()):
            bad.append(inner.strip()[:60])
    return bad


def render(body: str, values: dict[str, str | None]) -> str:
    """Substitute {{vars}} from values. Unknown vars render as empty string —
    deliberately boring: never raise, never execute, never leak keys."""
    def _sub(match: re.Match) -> str:
        name = match.group(1)
        value = values.get(name)
        if value is None:
            return ""
        return str(value)
    return VARIABLE_RE.sub(_sub, body or "")


def render_from_lead(body: str, lead) -> str:
    """Build the value map from a Lead row (None-safe)."""
    values = {name: getattr(lead, field, None) for name, field in RENDER_VARIABLES.items()}
    return render(body, values)


class TemplateService:
    # ------------------------------------------------------------- validation
    def validate_template(
        self, *, channel: str, subject: str | None, body: str,
        declared_variables: list[str] | None = None,
    ) -> dict:
        """Static validation (§10 + Phase 7 §11/§12/§39).
        Returns {valid, problems[], variables[], undeclared_variables[]}."""
        spec = get_channel(channel)
        if spec is None:
            return {"valid": False, "problems": [f"Unknown channel: {channel}"], "variables": []}
        channel = channel.upper()
        problems: list[str] = []
        if not (body or "").strip():
            problems.append("Template body must not be empty")
        if spec.template.get("requires_subject") and not (subject or "").strip():
            problems.append(f"{spec.name} templates require a subject")
        if not spec.template.get("requires_subject") and (subject or "").strip():
            problems.append(f"{spec.name} templates do not use a subject")
        max_len = spec.template.get("max_body_chars")
        if max_len and len(body or "") > max_len:
            problems.append(f"Body exceeds {spec.name} maximum of {max_len} characters")

        # §39: subjects must be free of header-injection control characters
        if subject and not header_safe(subject):
            problems.append("Subject contains forbidden control characters (CR/LF)")

        allowlist = EMAIL_RENDER_VARIABLES if channel == "EMAIL" else RENDER_VARIABLES
        used = extract_variables(body)
        if subject:
            used |= extract_variables(subject)
        for block in malformed_blocks(body) + (
            malformed_blocks(subject) if subject else []
        ):
            problems.append(f"Invalid template syntax: {{{{{block}}}}}")
        unknown = used - set(allowlist)
        for name in sorted(unknown):
            problems.append(f"Unknown variable: {{{{{name}}}}}")

        declared = set(declared_variables or [])
        # declared variables must be renderable too
        for name in sorted(declared - set(allowlist)):
            problems.append(f"Declared variable is not available: {name}")
        # every used variable should be declared (documentation aid, not fatal)
        undeclared = sorted(used - declared)
        return {
            "valid": not problems,
            "problems": problems,
            "variables": sorted(used),
            "undeclared_variables": undeclared,
        }

    def preview(self, template: CampaignTemplate, lead) -> dict:
        """Render preview with a sample lead (§10)."""
        subject = None
        if template.subject:
            subject = render_from_lead(template.subject, lead)
        body = render_from_lead(template.body, lead)
        return {"subject": subject, "body": body}

    # ------------------------------------------------------------------- CRUD
    async def create(
        self, session: AsyncSession, *,
        name: str, channel: str, body: str, subject: str | None = None,
        language: str = "en", status: str = TemplateStatus.DRAFT,
        variables: list[str] | None = None,
        text_body: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> CampaignTemplate:
        clean_name = " ".join(str(name or "").split())[:150]
        if not clean_name:
            raise ValidationError("Template name must not be empty")
        report = self.validate_template(
            channel=channel, subject=subject, body=body, declared_variables=variables
        )
        if not report["valid"]:
            raise ValidationError("Template validation failed", details={"problems": report["problems"]})
        status = (status or TemplateStatus.DRAFT).upper()
        if status not in (TemplateStatus.DRAFT, TemplateStatus.ACTIVE, TemplateStatus.ARCHIVED):
            raise ValidationError("status must be DRAFT, ACTIVE or ARCHIVED")
        components: dict = {}
        if channel.upper() == "EMAIL":
            # §38: template HTML is sanitized BEFORE storage and again at
            # render time; §10: optional explicit plain-text fallback
            body = sanitize_html(body)
            if text_body:
                components["text"] = text_body[:200_000]
        template = CampaignTemplate(
            name=clean_name, channel=channel.upper(), subject=(subject or None),
            body=body, status=status, language=language or "en",
            variables=report["variables"], components=components,
            created_by=created_by,
        )
        session.add(template)
        await session.commit()
        await session.refresh(template)
        return template

    async def get(self, session: AsyncSession, template_id: uuid.UUID) -> CampaignTemplate:
        template = await session.get(CampaignTemplate, template_id)
        if template is None:
            raise NotFoundError("Template not found")
        return template

    async def list(
        self, session: AsyncSession, *, channel: str | None = None,
        status: str | None = None, page: int = 1, page_size: int = 50,
    ) -> tuple[list[CampaignTemplate], int]:
        query = select(CampaignTemplate)
        if channel:
            query = query.where(CampaignTemplate.channel == channel.upper())
        if status:
            query = query.where(CampaignTemplate.status == status.upper())
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(CampaignTemplate.updated_at.desc())
            .offset(max(0, (page - 1)) * page_size).limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def update(
        self, session: AsyncSession, template_id: uuid.UUID, *,
        name: str | None = None, subject: str | None = None, body: str | None = None,
        status: str | None = None, language: str | None = None,
        variables: list[str] | None = None,
        text_body: str | None = None,
    ) -> CampaignTemplate:
        template = await self.get(session, template_id)
        if name is not None:
            clean = " ".join(str(name).split())[:150]
            if not clean:
                raise ValidationError("Template name must not be empty")
            template.name = clean
        if body is not None:
            template.body = (
                sanitize_html(body) if template.channel == "EMAIL" else body
            )
        if subject is not None:
            template.subject = subject or None
        if language is not None:
            template.language = language or template.language
        if variables is not None:
            template.variables = list(variables)
        if text_body is not None and template.channel == "EMAIL":
            template.components = {
                **(template.components or {}),
                "text": text_body[:200_000],
            }
        if status is not None:
            status = status.upper()
            if status not in (TemplateStatus.DRAFT, TemplateStatus.ACTIVE, TemplateStatus.ARCHIVED):
                raise ValidationError("status must be DRAFT, ACTIVE or ARCHIVED")
            template.status = status
        report = self.validate_template(
            channel=template.channel, subject=template.subject, body=template.body,
            declared_variables=template.variables,
        )
        if not report["valid"]:
            await session.rollback()
            raise ValidationError("Template validation failed", details={"problems": report["problems"]})
        await session.commit()
        await session.refresh(template)
        return template

    async def delete(self, session: AsyncSession, template_id: uuid.UUID) -> None:
        """Templates referenced by campaigns are archived, never destroyed
        (campaign reproducibility). Unreferenced templates are removed."""
        template = await self.get(session, template_id)
        from app.models.marketing import Campaign
        in_use = await session.scalar(
            select(func.count()).select_from(Campaign)
            .where(Campaign.template_id == template_id)
        )
        if in_use:
            template.status = TemplateStatus.ARCHIVED
            await session.commit()
            return
        await session.delete(template)
        await session.commit()
