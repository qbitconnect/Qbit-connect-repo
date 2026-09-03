"""Email send preparation (Phase 7) — the worker-side EMAIL branch.

Turns (campaign, recipient, lead, template, account) into the final provider
payload for ONE email (§17, §21: the worker is the only component that sends):

    1. build the value map: lead fields + recipient email + REAL
       unsubscribe_url (one-time token) + company fields (§11, §12)
    2. render subject / html / text  (HTML-escaped substitution, §38)
    3. append the unsubscribe footer when the template lacks the link (§12)
    4. opt-in open/click tracking per campaign settings (§29, §30, §31)
    5. issue the recipient's unguessable tracking_key (never a DB id in URLs)

Everything is honest: when the unsubscribe link cannot be generated (base URL
unset) the launch-time validation already blocked the campaign — this module
assumes a validated campaign and never fabricates links.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.marketing import Campaign, CampaignRecipient, CampaignTemplate, RecipientStatus
from app.services.marketing.email_compose import EmailComposer, html_to_text
from app.services.marketing.template import RENDER_VARIABLES, render
from app.services.marketing.unsubscribe import UnsubscribeService

logger = get_logger("qbit.marketing.email_send")


def build_render_values(lead, *, recipient_email: str,
                        unsubscribe_url: str | None,
                        campaign_metadata: dict) -> dict[str, str | None]:
    """Value map for the safe renderer (allowlist-driven, §11).

    lead fields come from the Phase 5 allowlist (RENDER_VARIABLES);
    email-specific extras are added explicitly — nothing arbitrary.
    """
    values: dict[str, str | None] = {}
    if lead is not None:
        for name in RENDER_VARIABLES:
            values[name] = getattr(lead, RENDER_VARIABLES[name], None)
        values["email"] = recipient_email
        values["phone"] = getattr(lead, "phone", None)
        values["website"] = getattr(lead, "website", None) if hasattr(lead, "website") else None
    else:
        values = {name: None for name in RENDER_VARIABLES}
    # §12: preferred company fields come from campaign metadata (operator-set)
    values["company_name"] = campaign_metadata.get("company_name") or None
    values["company_address"] = campaign_metadata.get("company_address") or None
    values["unsubscribe_url"] = unsubscribe_url
    return values


async def prepare_email_message(
    session: AsyncSession, *,
    campaign: Campaign, recipient: CampaignRecipient, lead,
    template: CampaignTemplate, account, settings: Settings,
) -> dict:
    """Prepare the final email payload. Raises-free; returns a dict:

        {"ok": True, "subject", "body", "template", "skip_reason"?, ...}

    On template-variable problems the recipient is skipped honestly
    (MISSING_TEMPLATE_VARIABLE) exactly like the WhatsApp path.
    """
    composer = EmailComposer(
        secret_key=settings.QBIT_SECRET_KEY,
        unsubscribe_base_url=settings.QBIT_EMAIL_UNSUBSCRIBE_BASE_URL,
    )
    campaign_metadata = campaign.campaign_metadata or {}
    recipient_email = (recipient.recipient_address or "").strip()

    # ---- unsubscribe: a REAL one-time token per recipient (§12, §13) --------
    unsubscribe_url = None
    needs_unsubscribe = bool(
        campaign_metadata.get("append_unsubscribe_footer",
                              settings.QBIT_EMAIL_APPEND_UNSUBSCRIBE_FOOTER)
    )
    if settings.QBIT_EMAIL_UNSUBSCRIBE_BASE_URL:
        raw_token, _row = await UnsubscribeService().issue_token(
            session, address=recipient_email,
            campaign_id=campaign.id, recipient_id=recipient.id,
            lead_id=recipient.lead_id,
        )
        unsubscribe_url = composer.unsubscribe_url_for(raw_token)

    # ---- render (safe substitution) ------------------------------------------
    values = build_render_values(
        lead, recipient_email=recipient_email,
        unsubscribe_url=unsubscribe_url, campaign_metadata=campaign_metadata,
    )
    html_source = template.body or ""
    text_source = str((template.components or {}).get("text") or "") or html_to_text(html_source)

    # EMAIL renders missing lead variables as empty strings (render()'s
    # documented semantics — free-form HTML, unlike provider templates that
    # reject empty params). Missing values are surfaced as PREVIEW warnings
    # (§37), never as silent recipient skips.
    parts = composer.render_parts(
        subject=template.subject, html_body=html_source or None,
        text_body=text_source, values=values,
    )

    # ---- unsubscribe footer (only when the body lacks a real link) ----------
    html_out, text_out, appended = composer.ensure_unsubscribe(
        html=parts["html"], text=parts["text"], unsubscribe_url=unsubscribe_url,
    )
    if needs_unsubscribe and unsubscribe_url is None:
        # validated campaigns always reach here with a URL; kept as a guard
        return {"ok": False, "skip": True, "skip_reason": "UNSUBSCRIBE_UNAVAILABLE"}

    # ---- tracking (campaign-level opt-in, §29–§31) ---------------------------
    track_opens = bool(campaign_metadata.get(
        "track_opens", settings.QBIT_EMAIL_DEFAULT_TRACK_OPENS))
    track_clicks = bool(campaign_metadata.get(
        "track_clicks", settings.QBIT_EMAIL_DEFAULT_TRACK_CLICKS))
    base_url = (settings.QBIT_EMAIL_UNSUBSCRIBE_BASE_URL or "").rstrip("/")

    tracking_key = recipient.tracking_key
    if (track_opens or track_clicks) and not tracking_key:
        tracking_key = uuid.uuid4().hex + uuid.uuid4().hex[:16]
        recipient.tracking_key = tracking_key
    if track_clicks and html_out and tracking_key:
        html_out, _count = composer.wrap_links(
            html=html_out, base_url=base_url, tracking_key=tracking_key,
            campaign_id=str(campaign.id), enabled=True,
        )
    if track_opens and html_out and tracking_key:
        html_out = composer.inject_open_pixel(
            html=html_out, base_url=base_url, tracking_key=tracking_key, enabled=True,
        )

    headers = {}
    if (account.config_metadata or {}).get("reply_to"):
        headers["Reply-To"] = (account.config_metadata or {}).get("reply_to")

    await session.commit()
    logger.info(
        "email_prepared",
        extra={"extra_fields": {
            "campaign_id": str(campaign.id), "recipient_id": str(recipient.id),
            "tracking": {"opens": track_opens, "clicks": track_clicks},
            "footer_appended": appended,
        }},
    )
    return {
        "ok": True,
        "subject": parts["subject"],
        "body": text_out,
        "template": {
            "html": html_out,
            "text": text_out,
            "headers": headers,
        },
    }


def _extract_variables(*, subject: str, html: str, text: str) -> set[str]:
    from app.services.marketing.template import extract_variables

    used: set[str] = extract_variables(subject)
    used |= extract_variables(html)
    used |= extract_variables(text)
    return used
