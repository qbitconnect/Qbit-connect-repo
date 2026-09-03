"""Sending account service (Phase 7 §2, §4–§7, §41).

Validation honesty rules (§7):
- an account is ACTIVE only after a real provider round-trip succeeds
- validation failures keep PENDING/ERROR with the provider's sanitized reason
- health checks update health_status + last_health_check; campaigns refuse
  to launch on UNHEALTHY accounts (§41)
- credentials: encrypted at rest (vault), never returned by API responses,
  never logged (§6)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger, log_with
from app.models.marketing import (
    AccountStatus,
    HealthStatus,
    SendingAccount,
)
from app.services.marketing.providers.registry import get_provider
from app.services.marketing.secrets import SecretVault, new_credential_ref

logger = get_logger("qbit.marketing.accounts")

EMAIL_PROVIDERS = ("smtp", "email_api", "mock_email")
WHATSAPP_PROVIDERS = ("whatsapp_cloud", "mock_whatsapp")

EMAIL_CAPABILITIES = {
    "smtp": {
        "supports_templates": True,     # local templates rendered by us
        "supports_media": False,
        "supports_inbound": False,      # reply mailbox ingestion is future work
        "supports_webhooks": False,
        "supports_tracking": True,
    },
    "email_api": {
        "supports_templates": True,
        "supports_media": False,
        "supports_inbound": False,
        "supports_webhooks": True,
        "supports_tracking": True,
    },
    "mock_email": {
        "supports_templates": True,
        "supports_media": False,
        "supports_inbound": False,
        "supports_webhooks": True,
        "supports_tracking": True,
    },
}
WHATSAPP_CAPABILITIES = {
    "whatsapp_cloud": {
        "supports_templates": True,     # provider-approved templates
        "supports_media": False,        # Phase 7 scope: TEXT TEMPLATE only
        "supports_inbound": True,
        "supports_webhooks": True,
        "supports_tracking": False,
    },
    "mock_whatsapp": {
        "supports_templates": True,
        "supports_media": False,
        "supports_inbound": True,
        "supports_webhooks": True,
        "supports_tracking": False,
    },
}


class SendingAccountService:
    def __init__(self, vault: SecretVault) -> None:
        self.vault = vault

    # ------------------------------------------------------------------ CRUD
    async def create(
        self,
        session: AsyncSession,
        *,
        channel: str,
        provider: str,
        name: str,
        config: dict,
        credentials: dict,
        sender_name: str | None = None,
        sender_email: str | None = None,
        reply_to: str | None = None,
        phone_number_id: str | None = None,
        business_account_id: str | None = None,
        created_by: uuid.UUID | None = None,
        is_production: bool = False,
    ) -> SendingAccount:
        channel = channel.upper()
        if channel == "EMAIL":
            if provider not in EMAIL_PROVIDERS:
                raise ValidationError(f"Unknown email provider '{provider}'")
            if not sender_email or "@" not in sender_email:
                raise ValidationError("sender_email is required for EMAIL accounts")
        elif channel == "WHATSAPP":
            if provider not in WHATSAPP_PROVIDERS:
                raise ValidationError(f"Unknown WhatsApp provider '{provider}'")
            if not phone_number_id:
                raise ValidationError("phone_number_id is required for WHATSAPP accounts")
        else:
            raise ValidationError(f"Unsupported channel: {channel}")
        if provider.startswith("mock_") and is_production:
            # Spec §55: mock providers are TEST ONLY — hard refusal in production.
            raise ValidationError(
                "Mock providers are forbidden in production; configure a real provider."
            )

        adapter = get_provider(channel, provider)
        outcome = await adapter.validate_configuration(config, credentials)
        if not outcome.ok:
            raise ValidationError(
                f"Provider configuration invalid: {outcome.message}",
                details={"code": outcome.code},
            )

        credential_ref = new_credential_ref(channel, name)
        await self.vault.put(
            session,
            ref=credential_ref,
            payload=credentials,
            description=f"{channel} credentials for {name}",
            created_by=created_by,
        )
        capabilities = (
            EMAIL_CAPABILITIES.get(provider, {}) if channel == "EMAIL" else WHATSAPP_CAPABILITIES.get(provider, {})
        )
        account = SendingAccount(
            name=name.strip(),
            channel=channel,
            provider=provider,
            status=AccountStatus.PENDING,
            health_status=HealthStatus.UNKNOWN,
            sender_name=sender_name,
            sender_email=sender_email,
            reply_to=reply_to,
            phone_number_id=phone_number_id,
            business_account_id=business_account_id,
            config=self._safe_config(channel, provider, config),
            capabilities=capabilities,
            credential_ref=credential_ref,
            created_by=created_by,
        )
        session.add(account)
        await session.flush()
        log_with(
            logger, 20, "email_account_created" if channel == "EMAIL" else "whatsapp_account_created",
            account_id=str(account.id), provider=provider,
        )
        return account

    async def update(
        self,
        session: AsyncSession,
        account: SendingAccount,
        *,
        name: str | None = None,
        config: dict | None = None,
        credentials: dict | None = None,
        sender_name: str | None = None,
        sender_email: str | None = None,
        reply_to: str | None = None,
        phone_number_id: str | None = None,
        business_account_id: str | None = None,
    ) -> SendingAccount:
        if name is not None:
            account.name = name.strip()
        if config is not None:
            account.config = self._safe_config(account.channel, account.provider, config)
        if credentials:
            adapter = get_provider(account.channel, account.provider)
            outcome = await adapter.validate_configuration(account.config or {}, credentials)
            if not outcome.ok:
                raise ValidationError(f"Provider configuration invalid: {outcome.message}")
            await self.vault.put(
                session, ref=account.credential_ref or new_credential_ref(account.channel, account.name),
                payload=credentials,
            )
        for field_name, value in (
            ("sender_name", sender_name),
            ("sender_email", sender_email),
            ("reply_to", reply_to),
            ("phone_number_id", phone_number_id),
            ("business_account_id", business_account_id),
        ):
            if value is not None:
                setattr(account, field_name, value)
        # any material change requires re-validation before ACTIVE
        if account.status == AccountStatus.ACTIVE:
            account.status = AccountStatus.PENDING
        await session.flush()
        return account

    async def delete(self, session: AsyncSession, account: SendingAccount) -> None:
        if account.credential_ref:
            await self.vault.delete(session, account.credential_ref)
        await session.delete(account)
        await session.flush()

    async def get(self, session: AsyncSession, account_id: uuid.UUID) -> SendingAccount:
        account = await session.get(SendingAccount, account_id)
        if account is None:
            raise NotFoundError("Sending account not found")
        return account

    async def list(
        self, session: AsyncSession, *, channel: str | None = None
    ) -> list[SendingAccount]:
        query = select(SendingAccount).order_by(SendingAccount.created_at.desc())
        if channel:
            query = query.where(SendingAccount.channel == channel.upper())
        return list((await session.scalars(query)).all())

    # ------------------------------------------------------------- validation
    async def validate(self, session: AsyncSession, account: SendingAccount) -> dict:
        """Full validation: configuration → sender → live connectivity.
        Never marks ACTIVE on failure (spec §7)."""
        adapter = get_provider(account.channel, account.provider)
        config = account.config or {}
        credentials = (await self.vault.get(session, account.credential_ref)) if account.credential_ref else {}
        if credentials is None:
            credentials = {}

        steps: dict = {}
        config_outcome = await adapter.validate_configuration(config, credentials)
        steps["configuration"] = {"ok": config_outcome.ok, "message": config_outcome.message}
        if not config_outcome.ok:
            account.status = AccountStatus.ERROR
            account.status_message = config_outcome.message
            return {"ok": False, "steps": steps}

        sender_outcome = await adapter.validate_sender(
            config,
            credentials,
            {
                "sender_email": account.sender_email,
                "sender_name": account.sender_name,
                "reply_to": account.reply_to,
                "phone_number_id": account.phone_number_id,
            },
        )
        steps["sender"] = {"ok": sender_outcome.ok, "message": sender_outcome.message}
        if not sender_outcome.ok:
            account.status = AccountStatus.ERROR
            account.status_message = sender_outcome.message
            return {"ok": False, "steps": steps}

        health = await adapter.health_check(config, credentials)
        steps["connectivity"] = {
            "ok": health.healthy,
            "degraded": health.degraded,
            "message": health.message,
        }
        now = datetime.now(timezone.utc)
        account.last_health_check = now
        if health.healthy:
            account.status = AccountStatus.ACTIVE
            account.health_status = HealthStatus.HEALTHY
            account.status_message = None
            account.last_health_message = None
        elif health.degraded:
            # connectivity reachable but not fully healthy — do NOT activate
            account.health_status = HealthStatus.DEGRADED
            account.status_message = health.message
            account.last_health_message = health.message
        else:
            account.status = AccountStatus.ERROR
            account.health_status = HealthStatus.UNHEALTHY
            account.status_message = health.message
            account.last_health_message = health.message
        await session.flush()
        log_with(
            logger, 20, "email_account_validated" if account.channel == "EMAIL" else "whatsapp_account_validated",
            account_id=str(account.id), ok=health.healthy,
        )
        return {"ok": health.healthy, "steps": steps}

    # ----------------------------------------------------------------- health
    async def health_check(self, session: AsyncSession, account: SendingAccount) -> dict:
        adapter = get_provider(account.channel, account.provider)
        credentials = (await self.vault.get(session, account.credential_ref)) if account.credential_ref else {}
        health = await adapter.health_check(account.config or {}, credentials or {})
        account.last_health_check = datetime.now(timezone.utc)
        account.last_health_message = health.message
        if health.healthy:
            account.health_status = HealthStatus.HEALTHY
            if account.status == AccountStatus.ACTIVE:
                account.status_message = None
        elif health.degraded:
            account.health_status = HealthStatus.DEGRADED
        else:
            account.health_status = HealthStatus.UNHEALTHY
        await session.flush()
        log_with(
            logger, 20, "email_account_health_check" if account.channel == "EMAIL" else "whatsapp_account_health_check",
            account_id=str(account.id), healthy=health.healthy,
        )
        return {
            "healthy": health.healthy,
            "degraded": health.degraded,
            "code": health.code,
            "message": health.message,
            "health_status": account.health_status,
        }

    async def get_credentials(self, session: AsyncSession, account: SendingAccount) -> dict:
        """Vault fetch for the send path. NEVER exposed through the API."""
        if not account.credential_ref:
            return {}
        credentials = await self.vault.get(session, account.credential_ref)
        return credentials or {}

    @staticmethod
    def _safe_config(channel: str, provider: str, config: dict) -> dict:
        """Only non-secret config keys are persisted on the account row."""
        allowed = {
            "smtp": {"host", "port", "security", "timeout_seconds"},
            "email_api": {"api_base_url", "timeout_seconds", "region"},
            "whatsapp_cloud": {"api_base_url", "api_version", "phone_number_id"},
            "mock_email": {"mock_ready"},
            "mock_whatsapp": {"mock_ready"},
        }.get(provider, set())
        cleaned = {k: v for k, v in (config or {}).items() if k in allowed}
        if provider == "whatsapp_cloud" and config:
            cleaned["phone_number_id"] = config.get("phone_number_id")
        return cleaned
