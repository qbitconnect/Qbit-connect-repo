"""Email provider package (Phase 7).

    BaseMarketingProvider
        +-- WhatsAppProvider        (Phase 6 — WhatsApp Business Cloud API)
        +-- SMTPProvider            (Phase 7 — real SMTP: TLS/STARTTLS)
        +-- GenericEmailAPIProvider (Phase 7 — transactional email HTTP API)
        +-- EmailMockProvider       (MOCK / TEST ONLY)
        +-- SMSProvider             (interface — later phase)

Exposes the adapters plus the email event normalizer used by the webhook
pipeline. All adapters keep campaign logic OUT of the provider layer.
"""

from app.services.marketing.providers.email.api import GenericEmailAPIProvider
from app.services.marketing.providers.email.errors import EmailErrorNormalizer
from app.services.marketing.providers.email.events import EmailEventNormalizer
from app.services.marketing.providers.email.mock import EmailMockProvider
from app.services.marketing.providers.email.smtp import SMTPProvider, header_safe

__all__ = [
    "EmailErrorNormalizer",
    "EmailEventNormalizer",
    "EmailMockProvider",
    "GenericEmailAPIProvider",
    "SMTPProvider",
    "header_safe",
]
