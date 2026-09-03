"""Email connection service — sender account lifecycle (Phase 7 §2, §6, §7).

Flow (§7 / §9 wizard):

    Add Email Sender Account
        ↓
    Provider Type           (smtp | email_api | email_mock)
    Sender Details          (sender_name / sender_email / reply_to)
    Provider Configuration  (SMTP host/port/security or API base/region)
    Credentials             (SMTP password / API key → ENCRYPTED vault)
        ↓
    Validate                (config → sender → connectivity → auth)
        ↓
    Health Check
        ↓
    ACTIVE                  (ONLY when every validation step passes)

An account that fails validation is marked ERROR with an honest, sanitized
status message — it is NEVER marked ACTIVE (§7). Secrets are write-only:
accepted, encrypted at rest, never returned, never logged (§6). This service
owns orchestration only — provider calls live in the adapters, secrets in the
CredentialVault, statuses on SendingAccount (reused from Phase 5/6).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.marketing import (
    AccountHealth,
    AccountStatus,
    SendingAccount,
)
from app.services.marketing.credentials import CredentialVault
from app.services.marketing.email_normalization import is_email
from app.services.marketing.providers import MarketingProviderRegistry
from app.services.marketing.providers.email.smtp import SECURITY_MODES

logger = get_logger("qbit.marketing.connections_email")

EMAIL_PROVIDERS = ("smtp", "email_api", "email_mock")

#: non-secret capability surface advertised for email accounts (§2)
DEFAULT_EMAIL_CAPABILITIES = {
    "supports_html": True,
    "supports_plain_text": True,
    "supports_templates": True,
    "supports_media": False,
    "supports_inbound": True,     # reply-tracking foundation (§32)
    "supports_webhooks": True,
    "supports_tracking": True,
}


async def resolve_email_credentials(
    session: AsyncSession, account: SendingAccount, settings: Settings,
) -> dict | None:
    """Decrypted credentials for THIS call only (§6).

    Order: encrypted vault (per-account) → env fallback (single-account dev
    convenience). Returns None when nothing is configured — callers fail
    honestly. The dict is consumed by the provider client and never logged,
    persisted, or echoed.
    """
    if account.credential_ref:
        vault = CredentialVault(settings.QBIT_SECRET_KEY)
        return await vault.resolve(session, name=account.credential_ref)
    if account.provider == "smtp":
        if settings.SMTP_PASSWORD:
            return {
                "smtp_username": settings.SMTP_USERNAME or "",
                "smtp_password": settings.SMTP_PASSWORD,
            }
        return None
    if account.provider == "email_api" and settings.EMAIL_API_KEY:
        return {"api_key": settings.EMAIL_API_KEY}
    return None


def email_account_config_for(account: SendingAccount, settings: Settings) -> dict:
    """NON-secret provider config for provider calls (safe to log/redact)."""
    config = dict(account.config_metadata or {})
    if account.provider == "smtp":
        config.setdefault("smtp_host", settings.SMTP_HOST)
        if settings.SMTP_PORT and not config.get("smtp_port"):
            config["smtp_port"] = settings.SMTP_PORT
        config.setdefault("smtp_security", settings.SMTP_SECURITY)
    elif account.provider == "email_api":
        config.setdefault("api_base_url", settings.EMAIL_API_BASE_URL)
        config.setdefault("region", settings.EMAIL_API_REGION)
    if account.identifier and not config.get("sender_email"):
        config["sender_email"] = account.identifier
    if account.credential_ref:
        config.setdefault("credential_ref", account.credential_ref)
    return config


def _require_email_address(value: str | None, field: str) -> str:
    clean = (value or "").strip()
    if not clean or not is_email(clean):
        raise ValidationError(f"{field} must be a valid email address")
    if "\r" in clean or "\n" in clean:
        raise ValidationError(f"{field} contains forbidden control characters")
    return clean


class EmailConnectionService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ create
    async def create_email_account(
        self, session: AsyncSession, *,
        name: str, provider: str,
        sender_name: str | None = None,
        sender_email: str | None = None,
        reply_to: str | None = None,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_security: str | None = None,
        api_base_url: str | None = None,
        region: str | None = None,
        credentials: dict | None = None,
        capabilities: dict | None = None,
        created_by: uuid.UUID | None = None,
    ) -> SendingAccount:
        """Create a PENDING email sender account (§2, §9). Credentials go
        straight into the encrypted vault — never onto the account row."""
        provider = (provider or self.settings.EMAIL_PROVIDER).strip().lower()
        if provider not in EMAIL_PROVIDERS:
            raise ValidationError(
                f"Unknown email provider '{provider}' — supported: {', '.join(EMAIL_PROVIDERS)}"
            )
        clean_name = " ".join(str(name or "").split())[:150]
        if not clean_name:
            raise ValidationError("Account name must not be empty")
        sender_email = _require_email_address(sender_email, "sender_email")
        if reply_to:
            reply_to = _require_email_address(reply_to, "reply_to")
        sender_name = " ".join(str(sender_name or "").split())[:200] or None

        config_metadata: dict = {
            "configured": False,
            "sender_name": sender_name,
            "reply_to": reply_to,
            "capabilities": {**DEFAULT_EMAIL_CAPABILITIES, **(capabilities or {})},
        }
        if provider == "smtp":
            host = (smtp_host or self.settings.SMTP_HOST or "").strip()
            if not host:
                raise ValidationError("smtp_host is required for SMTP sender accounts")
            security = (smtp_security or self.settings.SMTP_SECURITY or "STARTTLS").upper()
            if security not in SECURITY_MODES:
                raise ValidationError(
                    f"smtp_security must be one of {', '.join(SECURITY_MODES)}"
                )
            config_metadata.update({
                "smtp_host": host,
                "smtp_port": int(smtp_port or self.settings.SMTP_PORT or 0) or None,
                "smtp_security": security,
            })
            if smtp_port:
                config_metadata["smtp_port"] = int(smtp_port)
        elif provider == "email_api":
            base = (api_base_url or self.settings.EMAIL_API_BASE_URL or "").strip()
            if not base:
                raise ValidationError("api_base_url is required for Email API sender accounts")
            if not base.startswith(("http://", "https://")):
                raise ValidationError("api_base_url must be an http(s) URL")
            config_metadata["api_base_url"] = base
            if region:
                config_metadata["region"] = region.strip()[:64]

        vault = CredentialVault(self.settings.QBIT_SECRET_KEY)
        credential_ref = None
        if credentials:
            credential_row = await vault.store(
                session,
                name=f"email:{uuid.uuid4().hex[:12]}",
                provider=provider,
                payload=credentials,
                created_by=created_by,
            )
            credential_ref = credential_row.name

        account = SendingAccount(
            name=clean_name,
            channel="EMAIL",
            provider=provider,
            identifier=sender_email,
            display_identifier=sender_email,  # §8: sender email is display data
            credential_ref=credential_ref,
            status=AccountStatus.PENDING,
            capabilities={**DEFAULT_EMAIL_CAPABILITIES, **(capabilities or {})},
            config_metadata=config_metadata,
            health_status=AccountHealth.UNKNOWN,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        logger.info(
            "email_account_created",
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
        """§7 sender validation flow. Status becomes ACTIVE only when the
        provider confirms every step; otherwise ERROR + honest message."""
        provider = self._provider(account, registry)
        if provider is None or provider.interface_only:
            raise ValidationError(
                f"Provider '{account.provider}' does not support account validation"
            )
        config = email_account_config_for(account, self.settings)
        try:
            credentials = await resolve_email_credentials(session, account, self.settings)
        except NotFoundError:
            credentials = None
        if hasattr(provider, "validate_account"):
            report = await provider.validate_account(config, credentials)
        else:  # pragma: no cover — interface providers never reach here
            report = {"ok": False, "checked_at": datetime.now(timezone.utc).isoformat(),
                      "steps": {"configuration": {"ok": False, "detail": "unsupported provider"}}}

        now = datetime.now(timezone.utc)
        if report.get("ok"):
            account.status = AccountStatus.ACTIVE
            account.health_status = AccountHealth.HEALTHY
            account.config_metadata = {**config, "configured": True}
        else:
            account.status = AccountStatus.ERROR
            account.config_metadata = {**config, "configured": False}
            account.health_status = AccountHealth.UNHEALTHY
        account.last_health_check = now
        await session.commit()
        await session.refresh(account)
        logger.info(
            "email_account_validated",
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
        config = email_account_config_for(account, self.settings)
        try:
            credentials = await resolve_email_credentials(session, account, self.settings)
        except NotFoundError:
            credentials = None
        try:
            if provider is not None:
                result = await provider.health_check(config, credentials)
            else:
                result = {"health": "UNKNOWN", "detail": "Provider not registered"}
        except Exception:  # noqa: BLE001 — probes never raise
            logger.exception("Email health probe crashed")
            result = {"health": "UNHEALTHY", "detail": "Health probe failed"}

        health = str(result.get("health") or "UNKNOWN").upper()
        if health not in tuple(h.value for h in AccountHealth):
            health = AccountHealth.UNKNOWN.value
        account.health_status = health
        account.last_health_check = datetime.now(timezone.utc)
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
            account.config_metadata = {
                **email_account_config_for(account, self.settings), "configured": True,
            }
        await session.commit()
        await session.refresh(account)
        return result

    # ----------------------------------------------------------------- status
    async def set_status(
        self, session: AsyncSession, account: SendingAccount, status: str,
    ) -> SendingAccount:
        """§7 status transitions driven by operators (disable/re-enable)."""
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
        """Remove the account AND its encrypted credential row (§6)."""
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
            "email_account_removed",
            extra={"extra_fields": {"sending_account_id": str(account.id)}},
        )

    # -------------------------------------------------------------- credentials
    async def update_credentials(
        self, session: AsyncSession, account: SendingAccount, credentials: dict,
    ) -> None:
        """Rotate credentials (§6). Resets configured=False until revalidated."""
        if not credentials:
            raise ValidationError("Credential payload must not be empty")
        vault = CredentialVault(self.settings.QBIT_SECRET_KEY)
        name = account.credential_ref or f"email:{uuid.uuid4().hex[:12]}"
        row = await vault.store(
            session, name=name, provider=account.provider, payload=credentials,
        )
        account.credential_ref = row.name
        config = email_account_config_for(account, self.settings)
        account.config_metadata = {**config, "configured": False, "credential_ref": row.name}
        if account.status == AccountStatus.ACTIVE:
            account.status = AccountStatus.INACTIVE
        await session.commit()

    # ------------------------------------------------------------------ list
    async def list_email_accounts(
        self, session: AsyncSession, *,
        status: str | None = None, page: int = 1, page_size: int = 50,
    ) -> tuple[list[SendingAccount], int]:
        query = select(SendingAccount).where(SendingAccount.channel == "EMAIL")
        if status:
            query = query.where(SendingAccount.status == status.upper())
        from sqlalchemy import func

        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = (await session.execute(
            query.order_by(SendingAccount.created_at.desc())
            .offset(max(0, page - 1) * page_size).limit(page_size)
        )).scalars().all()
        return list(rows), int(total or 0)

    # ---------------------------------------------------------------- helpers
    def _provider(
        self, account: SendingAccount, registry: MarketingProviderRegistry | None,
    ):
        from app.services.marketing.providers import build_provider_registry

        reg = registry or build_provider_registry(self.settings)
        return reg.get(account.provider)
