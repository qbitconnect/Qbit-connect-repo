"""Provider registry (Phase 5 §3).

    MarketingProvider registry
        +-- WhatsAppProvider  (interface)
        +-- EmailProvider     (interface)
        +-- SMSProvider       (interface)
        +-- MockProvider      (MOCK / TEST ONLY — gated)

The registry NEVER auto-registers the mock provider outside isolated test
environments, and never raises for unknown ids — callers get None and decide
how to fail (honest "Provider not configured" UX instead of stack traces).
"""

from __future__ import annotations

from app.core.config import Settings
from app.services.marketing.providers.base import BaseMarketingProvider
from app.services.marketing.providers.interfaces import (
    EmailProvider,
    SMSProvider,
    WhatsAppProvider,
)
from app.services.marketing.providers.mock import MockProvider


class MarketingProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseMarketingProvider] = {}

    def register(self, provider: BaseMarketingProvider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str | None) -> BaseMarketingProvider | None:
        return self._providers.get(provider_id or "")

    def ids(self) -> list[str]:
        return sorted(self._providers)

    def summary(self) -> dict:
        return {
            provider_id: {
                "channel": provider.channel,
                "interface_only": provider.interface_only,
                "test_only": provider.test_only,
            }
            for provider_id, provider in sorted(self._providers.items())
        }


def build_provider_registry(settings: Settings) -> MarketingProviderRegistry:
    """Phase 5 registry: the three channel interfaces + the mock provider
    when (and only when) the environment allows it."""
    registry = MarketingProviderRegistry()
    registry.register(WhatsAppProvider())
    registry.register(EmailProvider())
    registry.register(SMSProvider())
    if settings.QBIT_ENV == "test" or (
        settings.QBIT_MARKETING_ALLOW_MOCK_PROVIDER
        and settings.QBIT_ENV != "production"
    ):
        registry.register(MockProvider())
    return registry
