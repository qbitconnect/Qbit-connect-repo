"""Channel-agnostic campaign service (Phase 7 §17, §21, §36, §41, §42).

CampaignService contains ZERO provider/SMTP/vendor code (spec §1, §17):
channel differences live entirely in provider adapters and per-channel
validators. Pipeline:

    validate → audience snapshot → eligibility → suppression → queue → worker

Durability rules (§20, §21, §56):
- recipients snapshot is batched (never loads all leads into RAM)
- a recipient row (QUEUED) is COMMITTED before enqueue; broker loss is
  recoverable by the sweep
- sends are idempotent via uq(campaign, recipient, message_version)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger, log_with
from app.models.marketing import (
    Campaign,
    CampaignEvent,
    CampaignRecipient,
    CampaignStatus,
    Channel,
    EventType,
    RecipientStatus,
    SendingAccount,
    MarketingTemplate,
    ProviderTemplateStatus,
    can_transition,
)
from app.models.scrape import Lead
from app.services.leads.filters import build_filter_condition
from app.services.marketing import templates as template_engine
from app.services.marketing.eligibility import (
    ACCOUNT_UNAVAILABLE,
    INVALID_TEMPLATE,
    PROVIDER_NOT_READY,
    consent_map,
    suppressions_for,
)
from app.services.marketing.normalization import normalize_email, normalize_phone
from app.services.marketing.providers.registry import get_provider
from app.services.marketing.queue import MarketingQueue

logger = get_logger("qbit.marketing.campaigns")

SNAPSHOT_BATCH = 500


class CampaignService:
    # ------------------------------------------------------------------ CRUD
    async def create(
        self,
        session: AsyncSession,
        *,
        name: str,
        channel: str,
        template_id: uuid.UUID | None = None,
        sending_account_id: uuid.UUID | None = None,
        audience: dict | None = None,
        schedule_at: datetime | None = None,
        rate_config: dict | None = None,
        track_opens: bool = False,
        track_clicks: bool = False,
        created_by: uuid.UUID | None = None,
    ) -> Campaign:
        channel = channel.upper()
        if channel not in (Channel.EMAIL.value, Channel.WHATSAPP.value):
            raise ValidationError(f"Unsupported campaign channel: {channel}")
        campaign = Campaign(
            name=name.strip(),
            channel=channel,
            template_id=template_id,
            sending_account_id=sending_account_id,
            audience=audience or {},
            schedule_at=schedule_at,
            rate_config=rate_config or {},
            track_opens=int(bool(track_opens)),
            track_clicks=int(bool(track_clicks)),
            created_by=created_by,
        )
        session.add(campaign)
        await session.flush()
        return campaign

    async def get(self, session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFoundError("Campaign not found")
        return campaign

    # -------------------------------------------------------------- validation
    async def validate(self, session: AsyncSession, campaign: Campaign) -> dict:
        """Full pre-launch validation. Returns {ok, issues:[{code,message}]}."""
        issues: list[dict] = []

        template = (
            await session.get(MarketingTemplate, campaign.template_id)
            if campaign.template_id
            else None
        )
        if template is None:
            issues.append({"code": INVALID_TEMPLATE, "message": "Campaign template is required"})
        else:
            if template.channel != campaign.channel:
                issues.append(
                    {"code": INVALID_TEMPLATE, "message": "Template channel does not match campaign channel"}
                )
            if campaign.channel == "EMAIL":
                if not (template.subject or "").strip():
                    issues.append({"code": INVALID_TEMPLATE, "message": "Template subject is empty"})
                if not (template.html_body or template.text_body):
                    issues.append({"code": INVALID_TEMPLATE, "message": "Template has no HTML or text body"})
                unknown = template_engine.validate_variable_usage(
                    template.subject, template.html_body, template.text_body
                )
                if unknown:
                    issues.append(
                        {"code": INVALID_TEMPLATE, "message": f"Unknown template variables: {', '.join(unknown)}"}
                    )
            else:
                # WhatsApp: provider requires APPROVED templates (spec §Templates).
                if template.provider_status != ProviderTemplateStatus.APPROVED.value:
                    issues.append(
                        {
                            "code": INVALID_TEMPLATE,
                            "message": f"WhatsApp template is not APPROVED by the provider "
                            f"(status: {template.provider_status})",
                        }
                    )
                if not (template.body or "").strip():
                    issues.append({"code": INVALID_TEMPLATE, "message": "Template body is empty"})

        account = (
            await session.get(SendingAccount, campaign.sending_account_id)
            if campaign.sending_account_id
            else None
        )
        if account is None:
            issues.append({"code": ACCOUNT_UNAVAILABLE, "message": "Sending account is required"})
        else:
            if account.channel != campaign.channel:
                issues.append({"code": ACCOUNT_UNAVAILABLE, "message": "Account channel mismatch"})
            if account.status != "ACTIVE":
                issues.append(
                    {
                        "code": "SENDING_ACCOUNT_UNHEALTHY",
                        "message": f"Sending account is {account.status}; validate/activate it first",
                    }
                )
            elif account.health_status == "UNHEALTHY":
                issues.append(
                    {
                        "code": "SENDING_ACCOUNT_UNHEALTHY",
                        "message": f"Sending account health is UNHEALTHY: {account.last_health_message or 'no detail'}",
                    }
                )
            else:
                provider = self._provider_or_issue(account, issues)
                if provider is not None:
                    caps = account.capabilities or {}
                    required = {
                        "supports_webhooks": campaign.channel in ("EMAIL", "WHATSAPP"),
                    }
                    for cap, needed in required.items():
                        if needed and caps.get(cap) is False:
                            issues.append(
                                {
                                    "code": PROVIDER_NOT_READY,
                                    "message": f"Provider capability missing: {cap}",
                                }
                            )
        return {"ok": not issues, "issues": issues}

    def _provider_or_issue(self, account: SendingAccount, issues: list[dict]):
        try:
            return get_provider(account.channel, account.provider)
        except Exception:
            issues.append(
                {"code": PROVIDER_NOT_READY, "message": f"Unknown provider '{account.provider}'"}
            )
            return None

    # -------------------------------------------------------- audience snapshot
    async def snapshot_audience(
        self, session: AsyncSession, campaign: Campaign, *, actor: uuid.UUID | None = None
    ) -> dict:
        """Batched recipient snapshot from the lead filter (spec §56: never
        loads the whole audience into RAM). Idempotent per idempotency_key."""
        channel = campaign.channel
        spec = (campaign.audience or {}).get("filter")
        condition = build_filter_condition(spec or {})
        # Only leads that actually have a usable channel address are considered;
        # missing addresses are recorded honestly at eligibility time, so the
        # snapshot filter requires a non-empty raw address.
        address_column = Lead.email if channel == "EMAIL" else Lead.phone
        base = (
            select(Lead.id, address_column)
            .where(condition, address_column.isnot(None), address_column != "")
            .order_by(Lead.created_at.asc(), Lead.id.asc())
        )

        created = 0
        seen = 0
        offset = 0
        while True:
            batch = (await session.execute(base.limit(SNAPSHOT_BATCH).offset(offset))).all()
            if not batch:
                break
            offset += len(batch)
            for lead_id, address in batch:
                seen += 1
                norm = self._normalize_address(channel, address)
                if norm is None:
                    # keep invalid addresses visible: they will be SKIPPed by
                    # eligibility with a proper reason code
                    norm = str(address).strip().lower()
                key = f"{campaign.id}:{lead_id}:v{campaign.message_version}"
                exists = await session.scalar(
                    select(CampaignRecipient.id).where(CampaignRecipient.idempotency_key == key)
                )
                if exists:
                    continue
                session.add(
                    CampaignRecipient(
                        campaign_id=campaign.id,
                        lead_id=lead_id,
                        address=str(address).strip(),
                        address_norm=norm,
                        status=RecipientStatus.PENDING,
                        idempotency_key=key,
                        message_version=campaign.message_version,
                    )
                )
                created += 1
            await session.flush()

        total = await session.scalar(
            select(func.count()).select_from(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id
            )
        )
        campaign.audience_total = int(total or 0)
        return {"seen": seen, "created": created, "total": int(total or 0)}

    @staticmethod
    def _normalize_address(channel: str, address: str) -> str | None:
        if channel == "EMAIL":
            return normalize_email(address).normalized
        return normalize_phone(address).normalized

    # -------------------------------------------------------------- eligibility
    async def run_eligibility(self, session: AsyncSession, campaign: Campaign) -> dict:
        """Batch pass over PENDING recipients applying the §15 chain."""
        suppression_map = await suppressions_for(session, campaign.channel)
        consent = await consent_map(session, campaign.channel)
        require_opt_in = bool((campaign.audience or {}).get("require_opt_in", True))

        eligible = skipped = 0
        batch_size = 500
        while True:
            rows = (
                await session.scalars(
                    select(CampaignRecipient)
                    .where(
                        CampaignRecipient.campaign_id == campaign.id,
                        CampaignRecipient.status == RecipientStatus.PENDING,
                    )
                    .limit(batch_size)
                )
            ).all()
            if not rows:
                break
            for recipient in rows:
                reason = self._eligibility_reason(
                    campaign,
                    recipient,
                    suppression_map,
                    consent,
                    require_opt_in,
                )
                if reason is None:
                    recipient.status = RecipientStatus.ELIGIBLE
                    recipient.reason = None
                    eligible += 1
                else:
                    recipient.status = RecipientStatus.SKIPPED
                    recipient.reason = reason
                    skipped += 1
            await session.flush()

        campaign.eligible_count += eligible
        campaign.skipped_count += skipped
        return {"eligible": eligible, "skipped": skipped}

    def _eligibility_reason(
        self,
        campaign: Campaign,
        recipient: CampaignRecipient,
        suppression_map: dict[str, str],
        consent: dict[str, str],
        require_opt_in: bool,
    ) -> str | None:
        if campaign.channel == "EMAIL":
            result = normalize_email(recipient.address)
            if not result.valid:
                return result.reason or "INVALID_EMAIL"
            norm = result.normalized or ""
        else:
            result = normalize_phone(recipient.address)
            if not result.valid:
                return result.reason or "INVALID_PHONE"
            norm = result.normalized or ""

        reason = suppression_map.get(norm)
        if reason == "UNSUBSCRIBED":
            return "UNSUBSCRIBED"
        if reason is not None:
            return "SUPPRESSED"

        if require_opt_in and consent.get(norm) != "OPTED_IN":
            return "NO_OPT_IN"
        return None

    # -------------------------------------------------------------------- queue
    async def queue_eligible(
        self,
        session: AsyncSession,
        campaign: Campaign,
        queue: MarketingQueue,
        *,
        commit: bool = True,
    ) -> dict:
        """ELIGIBLE → QUEUED + enqueue (committed rows first — DB-first)."""
        queued = 0
        recipient_ids: list[str] = []
        while True:
            rows = (
                await session.scalars(
                    select(CampaignRecipient)
                    .where(
                        CampaignRecipient.campaign_id == campaign.id,
                        CampaignRecipient.status == RecipientStatus.ELIGIBLE,
                    )
                    .limit(500)
                )
            ).all()
            if not rows:
                break
            for recipient in rows:
                # record_event performs the ELIGIBLE→QUEUED transition and the
                # queued_at stamp; duplicate calls are ignored forward-only.
                applied = await self.record_event(
                    session,
                    campaign,
                    recipient,
                    EventType.QUEUED,
                    commit=False,
                )
                if applied:
                    queued += 1
                recipient_ids.append(str(recipient.id))
            await session.flush()
        if commit:
            await session.commit()  # rows durable BEFORE broker push
            for rid in recipient_ids:
                await queue.enqueue(rid)
        return {"queued": queued}

    # ------------------------------------------------------------------ launch
    async def launch(
        self,
        session: AsyncSession,
        campaign: Campaign,
        queue: MarketingQueue,
        *,
        actor: uuid.UUID | None = None,
        validator: dict | None = None,
    ) -> dict:
        """Full pre-launch pipeline. Raises ValidationError on failed
        validation (worker/UI surfaces the honest issues list)."""
        result = validator or await self.validate(session, campaign)
        campaign.validation_result = result
        if not result.get("ok"):
            raise ValidationError(
                "Campaign validation failed",
                details={"issues": result.get("issues", [])},
            )
        if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.PAUSED):
            raise ValidationError(f"Campaign cannot launch from status {campaign.status}")

        snapshot = await self.snapshot_audience(session, campaign)
        eligibility = await self.run_eligibility(session, campaign)
        campaign.status = CampaignStatus.QUEUED
        campaign.started_at = campaign.started_at or datetime.now(timezone.utc)
        campaign.error_code = None
        campaign.error = None
        queued = await self.queue_eligible(session, campaign, queue)
        log_with(
            logger, 20, "email_campaign_launched",
            campaign_id=str(campaign.id), channel=campaign.channel,
            eligible=eligibility.get("eligible"), queued=queued.get("queued"),
        )
        return {
            "snapshot": snapshot,
            "eligibility": eligibility,
            "queued": queued,
            "validation": result,
        }

    # ------------------------------------------------------------------ control
    async def pause(self, session: AsyncSession, campaign: Campaign) -> None:
        if campaign.status not in (CampaignStatus.QUEUED, CampaignStatus.RUNNING):
            raise ValidationError(f"Cannot pause a campaign in status {campaign.status}")
        campaign.status = CampaignStatus.PAUSED

    async def resume(self, session: AsyncSession, campaign: Campaign, queue: MarketingQueue) -> int:
        if campaign.status != CampaignStatus.PAUSED:
            raise ValidationError("Only paused campaigns can be resumed")
        campaign.status = CampaignStatus.RUNNING
        # Re-enqueue anything left in QUEUED (worker drains it again).
        rows = (
            await session.scalars(
                select(CampaignRecipient.id).where(
                    CampaignRecipient.campaign_id == campaign.id,
                    CampaignRecipient.status == RecipientStatus.QUEUED,
                )
            )
        ).all()
        await session.commit()
        for rid in rows:
            await queue.enqueue(str(rid))
        return len(rows)

    async def cancel(self, session: AsyncSession, campaign: Campaign) -> int:
        if campaign.status in (CampaignStatus.COMPLETED, CampaignStatus.CANCELLED):
            raise ValidationError("Campaign already finished")
        cancelled = 0
        while True:
            rows = (
                await session.scalars(
                    select(CampaignRecipient)
                    .where(
                        CampaignRecipient.campaign_id == campaign.id,
                        CampaignRecipient.status.in_(
                            [RecipientStatus.QUEUED, RecipientStatus.ELIGIBLE]
                        ),
                    )
                    .limit(500)
                )
            ).all()
            if not rows:
                break
            for recipient in rows:
                recipient.status = RecipientStatus.SKIPPED
                recipient.reason = "CAMPAIGN_CANCELLED"
                cancelled += 1
            await session.flush()
        campaign.status = CampaignStatus.CANCELLED
        campaign.completed_at = datetime.now(timezone.utc)
        return cancelled

    async def requeue_failed(
        self, session: AsyncSession, campaign: Campaign
    ) -> dict:
        """Bump message_version and re-snapshot FAILED recipients (spec §20:
        version participates in the idempotency key → previously FAILED sends
        may be retried deliberately; never duplicated)."""
        campaign.message_version += 1
        failed = (
            await session.scalars(
                select(CampaignRecipient).where(
                    CampaignRecipient.campaign_id == campaign.id,
                    CampaignRecipient.status == RecipientStatus.FAILED,
                )
            )
        ).all()
        created = 0
        for old in failed:
            key = f"{campaign.id}:{old.lead_id or old.address_norm}:v{campaign.message_version}"
            exists = await session.scalar(
                select(CampaignRecipient.id).where(CampaignRecipient.idempotency_key == key)
            )
            if exists:
                continue
            session.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    lead_id=old.lead_id,
                    address=old.address,
                    address_norm=old.address_norm,
                    status=RecipientStatus.ELIGIBLE,
                    idempotency_key=key,
                    message_version=campaign.message_version,
                    max_attempts=old.max_attempts,
                )
            )
            created += 1
        await session.flush()
        return {"created": created, "message_version": campaign.message_version}

    # ------------------------------------------------------------- event stream
    async def record_event(
        self,
        session: AsyncSession,
        campaign: Campaign,
        recipient: CampaignRecipient | None,
        event_type: EventType | str,
        *,
        provider: str | None = None,
        provider_message_id: str | None = None,
        provider_event_id: str | None = None,
        payload: dict | None = None,
        commit: bool = False,
        new_status: str | None = None,
    ) -> bool:
        """Insert a CampaignEvent + advance the recipient state machine
        (forward-only) + bump the matching campaign counter. Returns False
        when the event would not advance state (duplicate webhook)."""
        event_type_value = event_type.value if isinstance(event_type, EventType) else str(event_type)
        if new_status is None:
            new_status = self._status_for_event(event_type_value)
        if recipient is not None and new_status is not None:
            current = recipient.status
            if current == new_status:
                # duplicate of an already-applied transition — the raw provider
                # event store keeps it; campaign events must not double-count
                return False
            if not can_transition(current, new_status):
                # out-of-order or duplicate — log honestly, do not apply
                log_with(
                    logger, 20, "campaign_event_ignored",
                    event_type=event_type_value,
                    current=current, new=new_status,
                    campaign_id=str(campaign.id),
                )
                return False
            self._apply_status(recipient, new_status)
            self._bump(session, campaign, current, new_status)

        session.add(
            CampaignEvent(
                campaign_id=campaign.id,
                recipient_id=recipient.id if recipient is not None else None,
                event_type=event_type_value,
                channel=campaign.channel,
                provider=provider,
                provider_message_id=provider_message_id,
                provider_event_id=provider_event_id,
                payload=payload or {},
            )
        )
        if commit:
            await session.commit()
        return True

    @staticmethod
    def _status_for_event(event_type: str) -> str | None:
        # OPENED maps to the READ state (email open == WhatsApp read receipt);
        # CLICKED carries no state transition — its counter is bumped directly.
        return {
            "QUEUED": RecipientStatus.QUEUED,
            "SENT": RecipientStatus.SENT,
            "DELIVERED": RecipientStatus.DELIVERED,
            "READ": RecipientStatus.READ,
            "OPENED": RecipientStatus.READ,
            "BOUNCED": RecipientStatus.BOUNCED,
            "COMPLAINED": RecipientStatus.COMPLAINED,
            "FAILED": RecipientStatus.FAILED,
        }.get(event_type)

    @staticmethod
    def _apply_status(recipient: CampaignRecipient, new_status: str) -> None:
        recipient.status = new_status
        now = datetime.now(timezone.utc)
        if new_status == RecipientStatus.QUEUED:
            recipient.queued_at = now
        elif new_status == RecipientStatus.SENT:
            recipient.sent_at = now
        elif new_status == RecipientStatus.DELIVERED:
            recipient.delivered_at = now
        elif new_status == RecipientStatus.READ:
            recipient.opened_at = now
        elif new_status == RecipientStatus.BOUNCED:
            recipient.bounced_at = now
        elif new_status == RecipientStatus.COMPLAINED:
            recipient.complained_at = now

    @staticmethod
    def _bump(session: AsyncSession, campaign: Campaign, old_status: str, new_status: str) -> None:
        """Counter maintenance: -1 when leaving an 'open' bucket, +1 on the new."""
        dec_map = {
            RecipientStatus.QUEUED: "queued_count",
            RecipientStatus.SENT: "sent_count",
            RecipientStatus.DELIVERED: "delivered_count",
            RecipientStatus.READ: "opened_count",
            RecipientStatus.BOUNCED: "bounced_count",
            RecipientStatus.COMPLAINED: "complained_count",
            RecipientStatus.FAILED: "failed_count",
        }
        inc_map = dict(dec_map)
        if old_status in dec_map:
            setattr(campaign, dec_map[old_status], max(0, getattr(campaign, dec_map[old_status]) - 1))
        if new_status in inc_map:
            setattr(campaign, inc_map[new_status], getattr(campaign, inc_map[new_status]) + 1)
