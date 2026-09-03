"""Marketing engine services (Phase 7): multi-channel EMAIL + WHATSAPP.

Sub-modules:
- secrets        encrypted-at-rest provider credential vault (Fernet)
- normalization  email/phone normalization + validation
- providers      BaseMarketingProvider + WhatsApp/SMTP/EmailAPI/Mock adapters
- templates      template CRUD, {{variable}} rendering, HTML sanitization
- suppression    suppression registry + unsubscribe processing
- eligibility    pre-queue eligibility chain with reason codes
- campaigns      channel-agnostic campaign service (no provider code)
- delivery       email send pipeline used by the worker
- webhooks       webhook verification + idempotent event application
- tracking       optional open/click tracking
- analytics      campaign analytics from real events
- accounts       sending account service (validate/health/CRUD)
- conversations  inbound message → conversation/lead matching
"""

from app.services.marketing.secrets import SecretVault, get_vault
from app.services.marketing.normalization import (
    normalize_email,
    normalize_phone,
    validate_email,
)

__all__ = [
    "SecretVault",
    "get_vault",
    "normalize_email",
    "normalize_phone",
    "validate_email",
]
