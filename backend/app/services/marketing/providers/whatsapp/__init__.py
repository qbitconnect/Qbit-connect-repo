"""WhatsApp provider adapter package (Phase 6).

    WhatsAppCloudClient   official Graph API wrapper (client.py)
    WhatsAppErrorNormalizer  provider error → canonical code + class (errors.py)
    WhatsAppProvider      real adapter for whatsapp_cloud (provider.py)
    WhatsAppMockProvider  test-only simulator (mock.py)
"""

from app.services.marketing.providers.whatsapp.client import WhatsAppCloudClient
from app.services.marketing.providers.whatsapp.errors import (
    NormalizedProviderError,
    WhatsAppErrorNormalizer,
)
from app.services.marketing.providers.whatsapp.mock import WhatsAppMockProvider
from app.services.marketing.providers.whatsapp.provider import (
    DEFAULT_CAPABILITIES,
    WhatsAppProvider,
    count_placeholders,
    normalize_provider_template,
)

__all__ = [
    "DEFAULT_CAPABILITIES",
    "NormalizedProviderError",
    "WhatsAppCloudClient",
    "WhatsAppErrorNormalizer",
    "WhatsAppMockProvider",
    "WhatsAppProvider",
    "count_placeholders",
    "normalize_provider_template",
]
