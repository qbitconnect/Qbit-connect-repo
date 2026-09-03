"""Connection service — WhatsApp sending account lifecycle (Phase 6 §5–§10).

Flow (§5):

    Add WhatsApp Business Account
        ↓
    Provider Configuration      (non-secret config + encrypted credentials)
        ↓
    Validate Credentials        (token accepted by provider?)
        ↓
    Validate Business Account   (WABA readable?)
        ↓
    Validate Phone Number       (phone number exists, quality probe)
        ↓
    Connection Test             (health check)
        ↓
    ACTIVE                      (ONLY when every validation step passes)

An account that fails validation is marked ERROR with an honest status
message — it is NEVER marked ACTIVE (§5). Provider errors are surfaced
sanitized, never bypassed (compliance requirement).

This service owns orchestration only: provider calls live in WhatsAppProvider,
secrets live in the CredentialVault, statuses live on SendingAccount.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import mask_phone
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.marketing import (
    AccountHealth,
    AccountStatus,
    CampaignTemplate,
    SendingAccount,
    TemplateStatus,
)
from app.models.messaging import ProviderCredentials
from app.services.marketing.credentials import CredentialVault
from app.services.marketing.phone import normalize_recipient_phone
from app.services.marketing.providers import MarketingProviderRegistry
from app.services.marketing.providers.whatsapp import (
    DEFAULT_CAPABILITIES,
    WhatsAppProvider,
)

logger = get_logger("qbit.marketing.connections")

WHATSAPP_PROVIDERS = ("whatsapp_cloud", "whatsapp_mock")


async def resolve_account_credentials(
    session: AsyncSession, account: SendingAccount, settings: Settings,
) -> dict | None:
    """Decrypted credentials for THIS call only (§4).

    Order: encrypted vault (per-account) → env fallback (single-account dev
    convenience). Returns None when nothing is configured — callers fail
    honestly. The dict is consumed by the provider client and never logged,
    persisted, or echoed.
    """
    if account.credential_ref:
        vault = CredentialVault(settings.QBIT_SECRET_KEY)
        return await vault.resolve(session, name=account.credential_ref)
    if account.provider == "whatsapp_cloud" and settings.WHATSAPP_ACCESS_TOKEN:
        creds: dict = {"access_token": settings.WHATSAPP_ACCESS_TOKEN}
        if settings.WHATSAPP_APP_SECRET:
            creds["app_secret"] = settings.WHATSAPP_APP_SECRET
        return creds
    return None


def account_config_for(account: SendingAccount, settings: Settings) -> dict:
    """NON-secret provider config for provider calls (safe to log/redact)."""
    config = dict(account.config_metadata or {})
    config.setdefault("api_base_url", settings.WHATSAPP_API_BASE_URL)
    config.setdefault("api_version", settings.WHATSAPP_API_VERSION)
    if account.phone_number_id and not config.get("phone_number_id"):
        config["phone_number_id"] = account.phone_number_id
    if account.business_account_id and not config.get("business_account_id"):
        config["business_account_id"] = account.business_account_id
    if account.credential_ref:
        config.setdefault("credential_ref", account.credential_ref)
    return config


class ConnectionService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ create
    async def create_whatsapp_account(
        self, session: AsyncSession, *,
        name: str, provider: str,
        phone_number_id: str | None = None,
        business_account_id: str | None = None,
        credentials: dict | None = None,
        capabilities: dict | None = None,
        created_by: uuid.UUID | None = None,
    ) -> SendingAccount:
        """Create a PENDING account. Credentials (if supplied) go straight
        into the encrypted vault — never onto the account row."""
        provider = (provider or self.settings.WHATSAPP_PROVIDER).strip()
        if provider not in WHATSAPP_PROVIDERS:
            raise ValidationError(
                f"Unknown WhatsApp provider '{provider}' — supported: {', '.join(WHATSAPP_PROVIDERS)}"
            )
        clean_name = " ".join(str(name or "").split())[:150]
        if not clean_name:
            raise ValidationError("Account name must not be empty")
        phone_number_id = (phone_number_id or "").strip() or None
        business_account_id = (business_account_id or "").strip() or None
        if provider != "whatsapp_mock" and not phone_number_id:
            raise ValidationError("phone_number_id is required for WhatsApp Business accounts")

        vault = CredentialVault(self.settings.QBIT_SECRET_KEY)
        credential_ref = None
        if credentials:
            credential_row = await vault.store(
                session,
                name=f"whatsapp:{uuid.uuid4().hex[:12]}",
                provider=provider,
                payload=credentials,
                created_by=created_by,
            )
            credential_ref = credential_row.name

        account = SendingAccount(
            name=clean_name,
            channel="WHATSAPP",
            provider=provider,
            identifier=phone_number_id or clean_name,
            display_identifier=mask_phone(phone_number_id) if phone_number_id else None,
            credential_ref=credential_ref,
            phone_number_id=phone_number_id,
            business_account_id=business_account_id,
            status=AccountStatus.PENDING,
            capabilities={**DEFAULT_CAPABILITIES, **(capabilities or {})},
            config_metadata={
                "configured": False,
                "api_base_url": self.settings.WHATSAPP_API_BASE_URL,
                "api_version": self.settings.WHATSAPP_API_VERSION,
                "capabilities": {**DEFAULT_CAPABILITIES, **(capabilities or {})},
            },
            health_status=AccountHealth.UNKNOWN,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        logger.info(
            "whatsapp_account_created",
            extra={"extra_fields": {
                "sending_account_id": str(account.id),
                "provider": provider,
                "has_credentials": bool(credential_ref),
            }},
        )
        return account

    # ---------------------------------------------------------------- validate
    async def validate_account(
        self, session: AsyncSession, account: SendingAccount, *,
        registry: MarketingProviderRegistry | None = None,
    ) -> dict:
        """§5 connection validation. Status becomes ACTIVE only when the
        provider confirms every step; otherwise ERROR + honest message."""
        provider = self._provider(account, registry)
        if not isinstance(provider, WhatsAppProvider):
            raise ValidationError(f"Provider '{account.provider}' does not support account validation")
        config = account_config_for(account, self.settings)
        try:
            credentials = await resolve_account_credentials(session, account, self.settings)
        except NotFoundError:
            # missing/broken credential reference → validate honestly as a
            # failed credentials step instead of crashing the endpoint
            credentials = None
        report = await provider.validate_account(config, credentials)

        now = datetime.now(timezone.utc)
        if report.get("ok"):
            account.status = AccountStatus.ACTIVE
            account.health_status = AccountHealth.HEALTHY
            account.config_metadata = {**config, "configured": True}
            # adopt the real phone number as the display identifier
            steps = report.get("steps") or {}
            phone_step = (steps.get("phone_number_valid") or {}).get("detail")
            if isinstance(phone_step, dict):
                raw_phone = phone_step.get("display_phone_number_masked")
            else:
                raw_phone = None
            if isinstance(raw_phone, str) and raw_phone:
                account.display_identifier = raw_phone
        else:
            account.status = AccountStatus.ERROR
            account.config_metadata = {**config, "configured": False}
            account.health_status = AccountHealth.UNHEALTHY
        account.last_health_check = now
        await session.commit()
        await session.refresh(account)
        logger.info(
            "whatsapp_account_validated",
            extra={"extra_fields": {
                "sending_account_id": str(account.id),
                "ok": bool(report.get("ok")),
            }},
        )
        return report

    # ------------------------------------------------------------------ health
    async def health_check(
        self, session: AsyncSession, account: SendingAccount, *,
        registry: MarketingProviderRegistry | None = None,
    ) -> dict:
        """§7 health probe; stores sanitized result on the account."""
        provider = self._provider(account, registry)
        config = account_config_for(account, self.settings)
        try:
            credentials = await resolve_account_credentials(session, account, self.settings)
        except NotFoundError:
            credentials = None
        try:
            if isinstance(provider, WhatsAppProvider):
                result = await provider.health_check(config, credentials)
            elif provider is not None:
                result = await provider.health_check(config)
            else:
                result = {"health": "UNKNOWN", "detail": "Provider not registered"}
        except Exception:  # noqa: BLE001 — probes never raise
            logger.exception("Health probe crashed")
            result = {"health": "UNHEALTHY", "detail": "Health probe failed"}

        health = str(result.get("health") or "UNKNOWN").upper()
        if health not in tuple(h.value for h in AccountHealth):
            health = AccountHealth.UNKNOWN.value
        account.health_status = health
        account.last_health_check = datetime.now(timezone.utc)
        # honest, sanitized error summary (never raw provider payloads)
        detail = result.get("detail")
        summary = detail if isinstance(detail, str) else None
        if summary:
            account.config_metadata = {
                **(account.config_metadata or {}),
                "last_health_error": summary[:300],
            }
        elif isinstance(account.config_metadata, dict) and account.config_metadata.get("last_health_error"):
            account.config_metadata = {
                **account.config_metadata,
                "last_health_error": None,
            }
        # a healthy probe activates a PENDING account that already validated
        if health == AccountHealth.HEALTHY.value and account.status == AccountStatus.PENDING:
            account.status = AccountStatus.ACTIVE
            account.config_metadata = {**account_config_for(account, self.settings), "configured": True}
        await session.commit()
        await session.refresh(account)
        return result

    # ----------------------------------------------------------------- status
    async def set_status(
        self, session: AsyncSession, account: SendingAccount, status: str,
    ) -> SendingAccount:
        """§6 status transitions driven by operators (disable/re-enable)."""
        status = (status or "").upper()
        valid = tuple(s.value for s in AccountStatus)
        if status not in valid:
            raise ValidationError(f"status must be one of: {', '.join(valid)}")
        if status == AccountStatus.ACTIVE and not (account.config_metadata or {}).get("configured"):
            raise ValidationError(
                "Account cannot be activated before provider validation succeeds"
            )
        account.status = status
        account.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(account)
        return account

    # ------------------------------------------------------------------ delete
    async def delete_account(self, session: AsyncSession, account: SendingAccount) -> None:
        """Remove the account AND its encrypted credential row (§31 Remove)."""
        if account.credential_ref:
            try:
                await CredentialVault(self.settings.QBIT_SECRET_KEY).delete(
                    session, name=account.credential_ref,
                )
            except NotFoundError:
                pass  # credential already gone — account removal continues
        await session.delete(account)
        await session.commit()
        logger.info(
            "whatsapp_account_removed",
            extra={"extra_fields": {"sending_account_id": str(account.id)}},
        )

    # -------------------------------------------------------------- credentials
    async def update_credentials(
        self, session: AsyncSession, account: SendingAccount, credentials: dict,
    ) -> ProviderCredentials:
        """Rotate credentials (§4). Resets configured=False until revalidated."""
        if not credentials:
            raise ValidationError("Credential payload must not be empty")
        vault = CredentialVault(self.settings.QBIT_SECRET_KEY)
        name = account.credential_ref or f"whatsapp:{uuid.uuid4().hex[:12]}"
        row = await vault.store(
            session, name=name, provider=account.provider,
            payload=credentials,
        )
        account.credential_ref = row.name
        config = account_config_for(account, self.settings)
        account.config_metadata = {**config, "configured": False, "credential_ref": row.name}
        if account.status == AccountStatus.ACTIVE:
            account.status = AccountStatus.INACTIVE
        await session.commit()
        await session.refresh(row)
        return row

    # ------------------------------------------------------------------ sync
    async def sync_templates(
        self, session: AsyncSession, account: SendingAccount, *,
        registry: MarketingProviderRegistry | None = None,
        max_templates: int = 1000,
    ) -> dict:
        """§9 template synchronization.

        Upserts provider templates by (account, provider_template_id):
        - provider fields refreshed (status/category/components/language/body)
        - operator customizations PRESERVED: `variables` mapping and a
          manually archived platform status are never overwritten
        - `last_synced_at` tracked per template
        """
        provider = self._provider(account, registry)
        if not isinstance(provider, WhatsAppProvider):
            raise ValidationError(f"Provider '{account.provider}' does not support template sync")
        config = account_config_for(account, self.settings)
        credentials = await resolve_account_credentials(session, account, self.settings)
        rows = await provider.fetch_templates(config, credentials)

        now = datetime.now(timezone.utc)
        created = updated = 0
        existing = {
            t.provider_template_id: t
            for t in (
                await session.execute(
                    select(CampaignTemplate).where(
                        CampaignTemplate.account_id == account.id,
                        CampaignTemplate.origin == "PROVIDER",
                    )
                )
            ).scalars().all()
            if t.provider_template_id
        }
        seen_ids: set[str] = set()
        for row in rows[:max_templates]:
            provider_template_id = row["provider_template_id"]
            if not provider_template_id:
                continue
            seen_ids.add(provider_template_id)
            body_text = self._body_text(row["components"].get("raw"))
            current = existing.get(provider_template_id)
            platform_status = (
                TemplateStatus.ACTIVE
                if row["provider_status"] == "APPROVED" else TemplateStatus.DRAFT
            )
            if current is None:
                session.add(CampaignTemplate(
                    name=row["name"][:150],
                    channel="WHATSAPP",
                    subject=None,
                    body=body_text or "",
                    status=platform_status,
                    language=row["language"] or "en",
                    variables=[],
                    origin="PROVIDER",
                    provider_template_id=provider_template_id,
                    provider_status=row["provider_status"],
                    category=row["category"],
                    components=row["components"],
                    account_id=account.id,
                    last_synced_at=now,
                    rejected_reason=row["rejected_reason"],
                ))
                created += 1
            else:
                current.provider_status = row["provider_status"]
                current.category = row["category"]
                current.components = row["components"]
                current.language = row["language"] or current.language
                current.body = body_text or current.body
                current.last_synced_at = now
                current.rejected_reason = row["rejected_reason"]
                if current.status != TemplateStatus.ARCHIVED:
                    current.status = platform_status
                updated += 1
        await session.commit()
        summary = {
            "fetched": len(rows), "created": created, "updated": updated,
            "synced_at": now.isoformat(),
        }
        logger.info(
            "whatsapp_templates_synced",
            extra={"extra_fields": {"sending_account_id": str(account.id), **summary}},
        )
        return summary

    async def list_account_templates(
        self, session: AsyncSession, account: SendingAccount, *,
        status: str | None = None,
    ) -> list[CampaignTemplate]:
        query = select(CampaignTemplate).where(
            CampaignTemplate.account_id == account.id,
            CampaignTemplate.origin == "PROVIDER",
        )
        if status:
            query = query.where(CampaignTemplate.provider_status == status.upper())
        rows = (await session.execute(
            query.order_by(CampaignTemplate.updated_at.desc())
        )).scalars().all()
        return list(rows)

    # ---------------------------------------------------------------- helpers
    def _provider(
        self, account: SendingAccount, registry: MarketingProviderRegistry | None,
    ):
        from app.services.marketing.providers import build_provider_registry

        reg = registry or build_provider_registry(self.settings)
        return reg.get(account.provider)

    @staticmethod
    def _body_text(raw_components: list | None) -> str | None:
        for component in raw_components or []:
            if str(component.get("type") or "").upper() == "BODY":
                return str(component.get("text") or "") or None
        return None
