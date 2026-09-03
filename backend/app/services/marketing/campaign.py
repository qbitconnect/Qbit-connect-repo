"""Campaign lifecycle service (Phase 5 §6, §12, §16, §22, §23, §26).

Status machine (platform-level, never provider-specific):

    DRAFT ──launch──▶ QUEUED ──worker──▶ RUNNING ──all sent──▶ COMPLETED
      │                  │                  │
      └──schedule──▶ SCHEDULED             PAUSED ◀──pause──┘
                          │                  │
                       (due)◀──resume───────┘
                          ▼
                        QUEUED

Any non-terminal state ──cancel──▶ CANCELLED; terminal states ──▶ ARCHIVED.

The HTTP layer only flips statuses and records events — the heavy work
(snapshot, eligibility, queueing, sending) happens in the worker loop
(§17: never inside HTTP request handlers).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.models.marketing import (
    Campaign,
    CampaignQueueItem,
    CampaignRecipient,
    CampaignStatus,
    CampaignTemplate,
    EventType,
    RecipientStatus,
    SendingAccount,
)
from app.services.marketing.audience import AudienceService
from app.services.marketing.channels import get_channel
from app.services.marketing.eligibility import EligibilityService
from app.services.marketing.events import EventService
from app.services.marketing.providers import MarketingProviderRegistry
from app.services.marketing.queue import QueueService
from app.models.scrape import Lead

logger = get_logger("qbit.marketing.campaign")

EDITABLE_STATUSES = {CampaignStatus.DRAFT, CampaignStatus.SCHEDULED}
ACTIVE_STATUSES = {CampaignStatus.QUEUED, CampaignStatus.RUNNING, CampaignStatus.PAUSED}
CANCELLABLE_STATUSES = {
    CampaignStatus.SCHEDULED, CampaignStatus.QUEUED, CampaignStatus.RUNNING, CampaignStatus.PAUSED,
}

VALIDATION_OK = "PASS"
VALIDATION_FAIL = "FAIL"


class CampaignService:
    def __init__(self) -> None:
        self.audience = AudienceService()
        self.eligibility = EligibilityService()
        self.queue = QueueService()
        self.events = EventService()

    # ------------------------------------------------------------------- CRUD
    async def create(
        self, session: AsyncSession, *,
        name: str, channel: str, description: str | None = None,
        audience_definition: dict | None = None,
        template_id: uuid.UUID | None = None,
        sending_account_id: uuid.UUID | None = None,
        schedule_type: str = "SEND_NOW",
        scheduled_at: datetime | None = None,
        timezone_name: str | None = None,
        created_by: uuid.UUID | None = None,
    ) -> Campaign:
        clean_name = " ".join(str(name or "").split())[:200]
        if not clean_name:
            raise ValidationError("Campaign name must not be empty")
        channel = (channel or "").upper()
        if get_channel(channel) is None:
            raise ValidationError(f"Unknown channel: {channel}")
        if audience_definition is not None:
            audience_definition = self.audience.validate_definition(audience_definition)
        if template_id is not None:
            await self._require_template(session, template_id, channel)
        if sending_account_id is not None:
            await self._require_account(session, sending_account_id, channel)
        schedule_type = (schedule_type or "SEND_NOW").upper()
        if schedule_type not in ("SEND_NOW", "SCHEDULED"):
            raise ValidationError("schedule_type must be SEND_NOW or SCHEDULED")
        if schedule_type == "SCHEDULED":
            if scheduled_at is None:
                raise ValidationError("SCHEDULED campaigns require scheduled_at")
            if scheduled_at.tzinfo is None:
                raise ValidationError("scheduled_at must include a timezone")
        campaign = Campaign(
            name=clean_name, description=description, channel=channel,
            status=CampaignStatus.DRAFT,
            audience_definition=audience_definition or {},
            template_id=template_id, sending_account_id=sending_account_id,
            schedule_type=schedule_type, scheduled_at=scheduled_at,
            timezone=timezone_name, created_by=created_by,
        )
        session.add(campaign)
        await session.commit()
        await session.refresh(campaign)
        await self.events.record(
            session, campaign_id=campaign.id, event_type=EventType.CAMPAIGN_CREATED,
            metadata={"name": campaign.name, "channel": campaign.channel},
        )
        logger.info(
            "campaign_created",
            extra={"extra_fields": {"campaign_id": str(campaign.id), "channel": campaign.channel}},
        )
        return campaign

    async def get(self, session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFoundError("Campaign not found")
        return campaign

    async def list(
        self, session: AsyncSession, *, status: str | None = None,
        channel: str | None = None, search: str | None = None,
        page: int = 1, page_size: int = 25,
    ) -> tuple[list[Campaign], int]:
        query = select(Campaign)
        if status:
            query = query.where(Campaign.status == status.upper())
        if channel:
            query = query.where(Campaign.channel == channel.upper())
        if search:
            query = query.where(Campaign.name.ilike(f"%{search[:100]}%"))
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(Campaign.created_at.desc())
            .offset(max(0, page - 1) * page_size).limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def update(
        self, session: AsyncSession, campaign_id: uuid.UUID, *,
        name: str | None = None, description: str | None = None,
        audience_definition: dict | None = None,
        template_id: uuid.UUID | None | str = "__unset__",
        sending_account_id: uuid.UUID | None | str = "__unset__",
        schedule_type: str | None = None,
        scheduled_at: datetime | None = None,
        timezone_name: str | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> Campaign:
        campaign = await self.get(session, campaign_id)
        if campaign.status not in EDITABLE_STATUSES:
            raise ConflictError(
                f"Campaign cannot be edited while {campaign.status} (DRAFT/SCHEDULED only)"
            )
        if name is not None:
            clean = " ".join(str(name).split())[:200]
            if not clean:
                raise ValidationError("Campaign name must not be empty")
            campaign.name = clean
        if description is not None:
            campaign.description = description
        if audience_definition is not None:
            campaign.audience_definition = self.audience.validate_definition(audience_definition)
        if template_id != "__unset__":
            if template_id in (None, ""):
                campaign.template_id = None
            else:
                template = await self._require_template(session, template_id, campaign.channel)
                campaign.template_id = template.id
        if sending_account_id != "__unset__":
            if sending_account_id in (None, ""):
                campaign.sending_account_id = None
            else:
                account = await self._require_account(session, sending_account_id, campaign.channel)
                campaign.sending_account_id = account.id
        if schedule_type is not None:
            schedule_type = schedule_type.upper()
            if schedule_type not in ("SEND_NOW", "SCHEDULED"):
                raise ValidationError("schedule_type must be SEND_NOW or SCHEDULED")
            campaign.schedule_type = schedule_type
        if scheduled_at is not None:
            if scheduled_at.tzinfo is None:
                raise ValidationError("scheduled_at must include a timezone")
            campaign.scheduled_at = scheduled_at
        if timezone_name is not None:
            campaign.timezone = timezone_name
        await session.commit()
        await session.refresh(campaign)
        return campaign

    # -------------------------------------------------------------- validation
    async def validate(
        self, session: AsyncSession, campaign_id: uuid.UUID, *,
        actor_id: uuid.UUID | None = None,
        provider_registry: MarketingProviderRegistry | None = None,
    ) -> dict:
        """Pre-launch validation report (§16) — read-only, no state change."""
        campaign = await self.get(session, campaign_id)
        report: dict = {"ok": True, "checks": {}}

        def _check(name: str, passed: bool, detail: str | None = None) -> None:
            report["checks"][name] = {"status": VALIDATION_OK if passed else VALIDATION_FAIL}
            if detail:
                report["checks"][name]["detail"] = detail
            if not passed:
                report["ok"] = False

        channel_ok = get_channel(campaign.channel) is not None
        _check("channel", channel_ok, campaign.channel)

        audience_count = 0
        try:
            audience_count = await self.audience.count(
                session, campaign.audience_definition or {},
                user_id=actor_id,
            )
            _check("audience", True, f"{audience_count} leads")
        except (ValidationError, NotFoundError, PermissionDeniedError) as exc:
            _check("audience", False, str(exc))
        report["audience_count"] = audience_count

        template = (
            await session.get(CampaignTemplate, campaign.template_id)
            if campaign.template_id else None
        )
        _check("template", template is not None and template.status == "ACTIVE",
               None if template else "No template selected")
        account = (
            await session.get(SendingAccount, campaign.sending_account_id)
            if campaign.sending_account_id else None
        )
        _check("sending_account", account is not None and account.status == "ACTIVE",
               None if account else "No sending account selected")
        # Phase 6 §29: an unhealthy account must never receive queued messages
        if account is not None and account.status == "ACTIVE":
            healthy = (account.health_status or "UNKNOWN") != "UNHEALTHY"
            _check("sending_account_health", healthy,
                   None if healthy else "SENDING_ACCOUNT_UNHEALTHY")
            # Phase 6 §27: campaign validation checks account capabilities
            caps = account.capabilities or {}
            caps_ok = caps.get("supports_templates", True) is not False
            _check("sending_account_capabilities", caps_ok,
                   None if caps_ok else "Account does not support template messaging")

        provider_ok = False
        if account is not None and provider_registry is not None:
            provider = provider_registry.get(account.provider)
            if provider is not None and not provider.interface_only:
                problems = await provider.validate_configuration(account.config_metadata or {})
                provider_ok = not problems
                if problems:
                    _check("provider", False, problems[0])
                # Phase 6 §8/§10: provider-level template requirements
                # (WhatsApp: provider-APPROVED template, variable count, ...)
                if template is not None and provider_ok:
                    template_problems = await provider.validate_send_requirements(
                        template=template, account_config=account.config_metadata or {},
                    )
                    _check("template_requirements", not template_problems,
                           template_problems[0] if template_problems else None)
            elif provider is not None:
                _check("provider", False,
                       f"Provider '{account.provider}' is not configured")
            else:
                _check("provider", False, f"Provider '{account.provider}' is not registered")
        if "provider" not in report["checks"]:
            _check("provider", provider_ok,
                   None if provider_ok else "Provider not configured")

        # eligibility preview over the whole audience (batched)
        eligible = skipped = suppressed = missing = no_opt_in = 0
        if audience_count:
            batch_size = 1000
            ids_q = await self.audience.build_condition(
                session, campaign.audience_definition or {}, user_id=actor_id
            )
            last_id = None
            while True:
                stmt = select(Lead.id).where(ids_q)
                if last_id is not None:
                    stmt = stmt.where(Lead.id > last_id)
                stmt = stmt.order_by(Lead.id).limit(batch_size)
                ids = (await session.execute(stmt)).scalars().all()
                if not ids:
                    break
                last_id = ids[-1]
                leads = (await session.execute(
                    select(Lead).where(Lead.id.in_(ids))
                )).scalars().all()
                results = await self.eligibility.check_batch(
                    session, channel=campaign.channel, leads=list(leads),
                    provider_registry=provider_registry,
                )
                for _lid, (status, reason) in results.items():
                    if status == "ELIGIBLE":
                        eligible += 1
                    else:
                        skipped += 1
                        if reason == "SUPPRESSED" or reason == "UNSUBSCRIBED":
                            suppressed += 1
                        elif reason in ("MISSING_PHONE", "MISSING_EMAIL", "INVALID_ADDRESS"):
                            missing += 1
                        elif reason == "NO_OPT_IN":
                            no_opt_in += 1
        report["eligibility"] = {
            "eligible": eligible, "skipped": skipped,
            "suppressed": suppressed, "missing_address": missing,
            "no_opt_in": no_opt_in,
        }
        _check("schedule", campaign.schedule_type != "SCHEDULED" or campaign.scheduled_at is not None,
               None if campaign.schedule_type != "SCHEDULED" else "scheduled_at missing")

        campaign.validation_report = report
        await session.commit()
        if report["ok"]:
            await self.events.record(
                session, campaign_id=campaign.id, event_type=EventType.CAMPAIGN_VALIDATED,
                metadata={"eligible": eligible, "skipped": skipped},
            )
        logger.info(
            "campaign_validated",
            extra={"extra_fields": {"campaign_id": str(campaign.id), "ok": report["ok"]}},
        )
        return report

    # ----------------------------------------------------------------- launch
    async def request_launch(
        self, session: AsyncSession, campaign_id: uuid.UUID, *,
        actor_id: uuid.UUID | None = None,
        provider_registry: MarketingProviderRegistry | None = None,
    ) -> Campaign:
        """HTTP entry point (§26): validate, then flip DRAFT/SCHEDULED → QUEUED.

        Heavy work happens in the worker. Provider MUST be configured — an
        unconfigured provider blocks launch with a clear error (§26, §35).
        """
        campaign = await self.get(session, campaign_id)
        if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.SCHEDULED):
            raise ConflictError(f"Campaign cannot launch from status {campaign.status}")
        report = await self.validate(
            session, campaign_id, actor_id=actor_id, provider_registry=provider_registry,
        )
        if not report["ok"]:
            raise ValidationError(
                "Campaign validation failed — resolve the reported checks before launch",
                details={"report": report},
            )
        eligible = report.get("eligibility", {}).get("eligible", 0)
        if eligible <= 0:
            raise ValidationError("No eligible recipients — nothing to send")
        if campaign.schedule_type == "SEND_NOW":
            campaign.status = CampaignStatus.QUEUED
        else:
            # SCHEDULED launch request = arm it; the scheduler flips it when due
            campaign.status = CampaignStatus.SCHEDULED
        await session.commit()
        await session.refresh(campaign)
        logger.info(
            "campaign_started",
            extra={"extra_fields": {"campaign_id": str(campaign.id),
                                    "status": campaign.status,
                                    "eligible": eligible}},
        )
        return campaign

    async def process_launch(
        self, session: AsyncSession, campaign: Campaign, *,
        actor_id: uuid.UUID | None = None,
        provider_registry: MarketingProviderRegistry | None = None,
        batch_size: int = 1000,
        max_audience: int | None = None,
    ) -> dict:
        """Worker-side launch: snapshot → eligibility → queue (§12, §17).

        Idempotent by construction: snapshot refuses double-run, queue insert
        is ON CONFLICT DO NOTHING.
        """
        snapshot = await self.audience.snapshot(
            session, campaign, user_id=actor_id,
            batch_size=batch_size, max_audience=max_audience,
        )
        if snapshot.get("already_snapshotted"):
            return snapshot

        # eligibility pass over the fresh snapshot, batched
        account = (
            await session.get(SendingAccount, campaign.sending_account_id)
            if campaign.sending_account_id else None
        )
        queued = 0
        last_id = None
        while True:
            stmt = select(CampaignRecipient.id).where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status == RecipientStatus.PENDING,
            )
            if last_id is not None:
                stmt = stmt.where(CampaignRecipient.id > last_id)
            stmt = stmt.order_by(CampaignRecipient.id).limit(batch_size)
            ids = (await session.execute(stmt)).scalars().all()
            if not ids:
                break
            last_id = ids[-1]
            recipients = (await session.execute(
                select(CampaignRecipient).where(CampaignRecipient.id.in_(ids))
            )).scalars().all()
            lead_ids = [r.lead_id for r in recipients if r.lead_id]
            leads = await self.eligibility.load_leads(session, lead_ids) if lead_ids else {}
            for recipient in recipients:
                lead = leads.get(recipient.lead_id) if recipient.lead_id else None
                status, reason = self.eligibility.check_recipient(
                    channel=campaign.channel, lead=lead,
                    suppressed=False, suppress_reason=None,
                )
                if status == "ELIGIBLE":
                    recipient.status = RecipientStatus.ELIGIBLE
                    recipient.eligibility_status = "ELIGIBLE"
                else:
                    recipient.status = RecipientStatus.INELIGIBLE
                    recipient.eligibility_status = "INELIGIBLE"
                    recipient.skip_reason = reason
                    await self.events.record(
                        session, campaign_id=campaign.id,
                        recipient_id=recipient.id,
                        event_type=EventType.RECIPIENT_SKIPPED,
                        metadata={"reason": reason}, commit=False,
                    )
            await session.commit()

            eligible_ids = [r.id for r in recipients if r.status == RecipientStatus.ELIGIBLE]
            if eligible_ids:
                created = await self.queue.enqueue(
                    session, campaign=campaign, recipient_ids=eligible_ids,
                    sending_account_id=account.id if account else None,
                    batch_size=batch_size,
                )
                queued += created
                await self.events.record(
                    session, campaign_id=campaign.id,
                    event_type=EventType.MESSAGE_QUEUED,
                    metadata={"count": created}, commit=False,
                )
                await session.commit()

        now = datetime.now(timezone.utc)
        campaign.status = CampaignStatus.RUNNING
        campaign.started_at = now
        await session.commit()
        await self.events.record(
            session, campaign_id=campaign.id, event_type=EventType.CAMPAIGN_STARTED,
            metadata={"snapshot": snapshot.get("total", 0), "queued": queued},
        )
        logger.info(
            "campaign_started",
            extra={"extra_fields": {"campaign_id": str(campaign.id),
                                    "recipients": snapshot.get("total", 0),
                                    "queued": queued}},
        )
        return {**snapshot, "queued": queued}

    # ----------------------------------------------------- pause/resume/cancel
    async def pause(self, session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
        campaign = await self.get(session, campaign_id)
        if campaign.status != CampaignStatus.RUNNING:
            raise ConflictError(f"Only RUNNING campaigns can be paused (currently {campaign.status})")
        campaign.status = CampaignStatus.PAUSED
        await session.commit()
        await self.events.record(session, campaign_id=campaign.id, event_type=EventType.CAMPAIGN_PAUSED)
        return campaign

    async def resume(self, session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
        campaign = await self.get(session, campaign_id)
        if campaign.status != CampaignStatus.PAUSED:
            raise ConflictError(f"Only PAUSED campaigns can be resumed (currently {campaign.status})")
        campaign.status = CampaignStatus.RUNNING
        await session.commit()
        await self.events.record(session, campaign_id=campaign.id, event_type=EventType.CAMPAIGN_RESUMED)
        return campaign

    async def cancel(self, session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
        campaign = await self.get(session, campaign_id)
        if campaign.status not in CANCELLABLE_STATUSES:
            raise ConflictError(f"Campaign cannot be cancelled from status {campaign.status}")
        campaign.status = CampaignStatus.CANCELLED
        campaign.completed_at = datetime.now(timezone.utc)
        await session.commit()
        cancelled = await self.queue.cancel_pending(session, campaign_id=campaign.id)
        await self.events.record(
            session, campaign_id=campaign.id, event_type=EventType.CAMPAIGN_CANCELLED,
            metadata={"cancelled_queue_items": cancelled},
        )
        # recipients still pending/queued are marked CANCELLED (already-sent
        # provider events are never modified, §23)
        await session.execute(
            select(CampaignRecipient.id).where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status.in_([
                    RecipientStatus.PENDING, RecipientStatus.ELIGIBLE, RecipientStatus.QUEUED,
                ]),
            )
        )
        from sqlalchemy import update as _update
        await session.execute(
            _update(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status.in_([
                    RecipientStatus.PENDING, RecipientStatus.ELIGIBLE, RecipientStatus.QUEUED,
                ]),
            )
            .values(status=RecipientStatus.CANCELLED, updated_at=datetime.now(timezone.utc))
        )
        await session.commit()
        return campaign

    async def archive(self, session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
        campaign = await self.get(session, campaign_id)
        if campaign.status not in (
            CampaignStatus.COMPLETED, CampaignStatus.CANCELLED, CampaignStatus.FAILED,
            CampaignStatus.DRAFT,
        ):
            raise ConflictError(f"Campaign cannot be archived from status {campaign.status}")
        campaign.status = CampaignStatus.ARCHIVED
        await session.commit()
        return campaign

    # ------------------------------------------------------------- scheduler
    async def process_due_schedules(self, session: AsyncSession) -> list[Campaign]:
        """SCHEDULED campaigns whose time has arrived → QUEUED (§22)."""
        now = datetime.now(timezone.utc)
        due = (await session.execute(
            select(Campaign).where(
                Campaign.status == CampaignStatus.SCHEDULED,
                Campaign.schedule_type == "SCHEDULED",
                Campaign.scheduled_at.isnot(None),
                Campaign.scheduled_at <= now,
            )
        )).scalars().all()
        for campaign in due:
            campaign.status = CampaignStatus.QUEUED
        if due:
            await session.commit()
        return list(due)

    # ----------------------------------------------------------------- helpers
    async def _require_template(
        self, session: AsyncSession, template_id, channel: str,
    ) -> CampaignTemplate:
        try:
            tid = template_id if isinstance(template_id, uuid.UUID) else uuid.UUID(str(template_id))
        except (ValueError, TypeError) as exc:
            raise ValidationError("template_id must be a UUID") from exc
        template = await session.get(CampaignTemplate, tid)
        if template is None:
            raise NotFoundError("Template not found")
        if template.channel != channel:
            raise ValidationError(f"Template channel {template.channel} does not match campaign channel {channel}")
        if template.status == "ARCHIVED":
            raise ValidationError("Archived templates cannot be used")
        return template

    async def _require_account(
        self, session: AsyncSession, account_id, channel: str,
    ) -> SendingAccount:
        try:
            aid = account_id if isinstance(account_id, uuid.UUID) else uuid.UUID(str(account_id))
        except (ValueError, TypeError) as exc:
            raise ValidationError("sending_account_id must be a UUID") from exc
        account = await session.get(SendingAccount, aid)
        if account is None:
            raise NotFoundError("Sending account not found")
        if account.channel != channel:
            raise ValidationError(f"Sending account channel {account.channel} does not match campaign channel {channel}")
        if account.status not in ("ACTIVE", "PENDING"):
            raise ValidationError(f"Sending account is {account.status}")
        return account
