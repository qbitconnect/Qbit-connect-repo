"""Marketing provider adapters (Phase 7)."""

from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    HealthOutcome,
    NormalizedEvent,
    OutboundMessage,
    SendResult,
    ValidationOutcome,
)
from app.services.marketing.providers.registry import (
    available_providers,
    get_provider,
    register,
    reset_registry,
)

__all__ = [
    "BaseMarketingProvider",
    "HealthOutcome",
    "NormalizedEvent",
    "OutboundMessage",
    "SendResult",
    "ValidationOutcome",
    "available_providers",
    "get_provider",
    "register",
    "reset_registry",
]
