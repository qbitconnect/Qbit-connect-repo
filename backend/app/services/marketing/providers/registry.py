"""Provider registry: resolve the adapter for a SendingAccount (Phase 7 §3).

Providers are pluggable adapters behind BaseMarketingProvider — the campaign
engine never imports a concrete vendor module. TEST-ONLY mock adapters are
hard-refused in production environments (spec §55, §64).
"""

from __future__ import annotations

from app.core.errors import ValidationError
from app.services.marketing.providers.base import BaseMarketingProvider
from app.services.marketing.providers.email_api import GenericEmailAPIProvider
from app.services.marketing.providers.smtp import SMTPProvider
from app.services.marketing.providers.whatsapp import WhatsAppProvider

_PROVIDERS: dict[tuple[str, str], BaseMarketingProvider] = {}


def register(provider: BaseMarketingProvider) -> None:
    _PROVIDERS[(provider.channel, provider.provider_id)] = provider


def _bootstrap() -> None:
    if not _PROVIDERS:
        register(SMTPProvider())
        register(GenericEmailAPIProvider())
        register(WhatsAppProvider())
        from app.services.marketing.providers.mock import (
            MockEmailProvider,
            MockWhatsAppProvider,
        )

        register(MockEmailProvider())
        register(MockWhatsAppProvider())


def get_provider(channel: str, provider_id: str, *, is_production: bool = False) -> BaseMarketingProvider:
    _bootstrap()
    key = (channel.upper(), provider_id)
    provider = _PROVIDERS.get(key)
    if provider is None:
        raise ValidationError(f"Unknown provider '{provider_id}' for channel '{channel}'")
    if provider.is_mock and is_production:
        raise ValidationError(
            "Mock providers are forbidden in production (spec §55): "
            "configure a real provider for this account."
        )
    return provider


def available_providers(channel: str) -> list[dict]:
    _bootstrap()
    return [
        {"provider_id": p.provider_id, "channel": p.channel, "is_mock": p.is_mock}
        for (chan, _), p in sorted(_PROVIDERS.items())
        if chan == channel.upper()
    ]


def reset_registry() -> None:
    """Test helper: forget cached instances (mock state is per-instance)."""
    _PROVIDERS.clear()
