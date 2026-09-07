"""Communication actions (§26, §27, §53, §54).

SEND_WHATSAPP / SEND_EMAIL reuse the EXISTING messaging architecture —
`ReplyService.queue_reply` (Phase 8) — which enforces account health, provider
availability, recipient validity, the suppression gate, the WhatsApp 24-hour
window rule and outbound idempotency, and delivers through the outbox worker
via the SAME provider abstraction campaigns use. The automation engine never
talks to providers directly and can never bypass eligibility (§26, §27).

Safety ladder (§27) — checked BEFORE queueing:
    conversation context → recipient address → suppression/unsubscribe →
    channel window/template requirements → queue via ReplyService.

Any failed check ⇒ the step is SKIPPED with a machine-readable reason —
the execution continues on its path (e.g. toward an END node). Messages are
idempotent per (execution, node): retries can never duplicate a send (§39,
§77).

START_CAMPAIGN (§54) goes through `CampaignService.request_launch` — the
existing two-stage, validation-gated, idempotent launch path. It NEVER calls
the worker-side process_launch directly and NEVER bypasses audience,
eligibility or rate gates. A validation failure SKIPS the step with the
report reasons.
"""

from __future__ import annotations

from typing import Any

from app.automation.actions._common import as_uuid, require_conversation
from app.automation.core.action import BaseAction
from app.automation.core.exceptions import ActionSkipped, ConfigurationError
from app.core.errors import ConflictError, NotFoundError, ValidationError


def _reason_from_conflict(message: str) -> str:
    """Map ReplyService conflict messages to stable skip reasons (TEST 5)."""
    text = str(message)
    head = text.split(":", 1)[0].strip().upper()
    if "UNSUBSCRIB" in head or "UNSUBSCRIB" in text.upper():
        return "UNSUBSCRIBED"
    if "SUPPRESSED" in head:
        return "SUPPRESSED"
    return head or "RECIPIENT_SUPPRESSED"


class _SendReplyAction(BaseAction):
    CHANNEL: str = ""

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        body = config.get("body")
        template_id = config.get("template_id")
        if not body and not template_id:
            raise ConfigurationError(
                f"{self.ACTION_KEY} requires 'body' (variables allowed) or 'template_id'"
            )
        if body is not None and not isinstance(body, str):
            raise ConfigurationError("'body' must be a string")
        if template_id is not None:
            tid = as_uuid(template_id)
            if session is not None:
                from app.models.marketing import CampaignTemplate, TemplateStatus

                row = session.get(CampaignTemplate, tid)
                if row is None or row.status != TemplateStatus.ACTIVE.value:
                    raise ConfigurationError(
                        f"Template {template_id!r} does not exist or is not ACTIVE "
                        "(missing template blocks publish — §46)"
                    )
                expected = self.CHANNEL
                if row.channel != expected:
                    raise ConfigurationError(
                        f"Template channel must be {expected} (got {row.channel!r})"
                    )

    async def _precheck(self, context, conversation) -> str:
        """Safety ladder (§27). Returns the recipient address; raises
        ActionSkipped when the send must not happen."""
        from app.models.scrape import Lead
        from app.services.marketing.suppression import SuppressionService

        address = (
            conversation.contact_email if self.CHANNEL == "email"
            else conversation.contact_phone
        )
        if not address:
            raise ActionSkipped(
                "MISSING_EMAIL" if self.CHANNEL == "email" else "MISSING_PHONE"
            )

        if conversation.lead_id:
            lead = await context.session.get(Lead, conversation.lead_id)
            if lead is not None and lead.archived_at is not None:
                raise ActionSkipped("LEAD_ARCHIVED")

        suppressed, reason = await SuppressionService().is_suppressed(
            context.session,
            channel=self.CHANNEL,
            email=address if self.CHANNEL == "email" else None,
            phone=address if self.CHANNEL == "whatsapp" else None,
            lead_id=conversation.lead_id,
        )
        if suppressed:
            # honest reason (UNSUBSCRIBED / SUPPRESSED) — never send (§27, §53)
            raise ActionSkipped(reason or "SUPPRESSED")
        return address

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        conversation = await context.load_conversation()
        if conversation is None:
            # honest skip — automation never invents conversations (§26/§27)
            raise ActionSkipped("NO_CONVERSATION_CONTEXT")
        await self._precheck(context, conversation)

        # WhatsApp window rule: without an approved template, outside-window
        # sends must SKIP (never bypass — §27, Phase 6 compliance boundary).
        if self.CHANNEL == "whatsapp" and not config.get("template_id"):
            from app.services.inbox.engine import ConversationEngine

            hours = getattr(context.settings, "QBIT_INBOX_WHATSAPP_WINDOW_HOURS", 24)
            if not ConversationEngine.within_whatsapp_window(conversation, hours=hours):
                raise ActionSkipped("WHATSAPP_WINDOW_CLOSED_TEMPLATE_REQUIRED")

        body = config.get("body")
        if body:
            rendered, unresolved = context.render(body)
        else:
            rendered, unresolved = "", []

        from app.services.inbox.engine import ConversationEngine
        from app.services.inbox.reply import ReplyService

        client_message_id = f"wf:{context.execution.id}:{getattr(context, '_current_node_id', 'node')}"
        try:
            message, created = await ReplyService(ConversationEngine()).queue_reply(
                context.session,
                conversation,
                user_id=None,
                body=rendered or None,
                subject=(context.render(config["subject"])[0] if config.get("subject") else None),
                client_message_id=client_message_id,
                template_id=as_uuid(config["template_id"]) if config.get("template_id") else None,
                settings=context.settings,
                commit=True,
            )
        except ConflictError as exc:
            raise ActionSkipped(_reason_from_conflict(str(exc))) from exc
        except ValidationError as exc:
            raise ConfigurationError(str(exc)) from exc
        except NotFoundError as exc:
            raise ConfigurationError(str(exc)) from exc

        # created=False ⇒ the SAME client_message_id was already queued —
        # a retry, not a duplicate send (§39)
        return {"status": "success", "result": {
            "message_id": str(message.id),
            "queued": bool(created),
            "duplicate_suppressed": not created,
            "unresolved_variables": unresolved,
        }}


class SEND_WHATSAPP(_SendReplyAction):
    ACTION_KEY = "send_whatsapp"
    LABEL = "Send WhatsApp message"
    CHANNEL = "whatsapp"


class SEND_EMAIL(_SendReplyAction):
    ACTION_KEY = "send_email"
    LABEL = "Send email"
    CHANNEL = "email"


class START_CAMPAIGN(BaseAction):
    ACTION_KEY = "start_campaign"
    LABEL = "Start a campaign"

    def validate_config(self, config: dict[str, Any], session: Any = None) -> None:
        from app.core.config import get_settings

        if not getattr(get_settings(), "QBIT_AUTOMATION_ENABLE_START_CAMPAIGN", True):
            raise ConfigurationError("start_campaign is disabled by configuration")
        if not config.get("campaign_id"):
            raise ConfigurationError("start_campaign requires campaign_id")
        if session is not None:
            from app.models.marketing import Campaign

            campaign = session.get(Campaign, as_uuid(config["campaign_id"]))
            if campaign is None:
                raise ConfigurationError(
                    f"Campaign {config['campaign_id']!r} does not exist (§46)"
                )

    async def execute(self, context, config: dict[str, Any]) -> dict[str, Any]:
        from app.models.marketing import Campaign
        from app.services.marketing.campaign import CampaignService
        from app.services.marketing.providers import build_provider_registry

        campaign_id = as_uuid(config.get("campaign_id"))
        campaign = await context.session.get(Campaign, campaign_id)
        if campaign is None:
            raise ActionSkipped("CAMPAIGN_NOT_FOUND")
        if campaign.status not in ("DRAFT", "SCHEDULED"):
            # already launched/finished — idempotent, honest skip (§54)
            raise ActionSkipped(f"CAMPAIGN_ALREADY_{campaign.status}")

        from app.models.automation import Workflow as WorkflowModel

        workflow_row = await context.session.get(
            WorkflowModel, context.execution.workflow_id
        )
        actor_id = workflow_row.created_by if workflow_row else None

        service = CampaignService()
        try:
            await service.request_launch(
                context.session,
                campaign_id,
                actor_id=actor_id,
                provider_registry=build_provider_registry(context.settings),
                settings=context.settings,
            )
        except ValidationError as exc:
            # validation report reasons → step SKIPPED (never partial launch)
            details = getattr(exc, "details", None) or {}
            report = details.get("report") if isinstance(details, dict) else None
            failed_checks = []
            if isinstance(report, dict):
                failed_checks = [
                    k for k, v in (report.get("checks") or {}).items()
                    if isinstance(v, dict) and not v.get("ok", True)
                ]
            raise ActionSkipped(
                "CAMPAIGN_VALIDATION_FAILED:" + (",".join(failed_checks) or "UNKNOWN")
            ) from exc
        except ConflictError as exc:
            raise ActionSkipped("CAMPAIGN_NOT_LAUNCHABLE") from exc
        return {"status": "success", "result": {
            "campaign_id": str(campaign_id), "launch_requested": True}}
