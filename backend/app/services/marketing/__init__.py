"""Marketing engine services (Phase 5 + Phase 6 WhatsApp provider integration).

    audience      lead → recipient resolution + immutable snapshot
    campaign      campaign lifecycle (create/validate/launch/pause/…)
    channels      WHATSAPP / EMAIL / SMS declarations
    connections   WhatsApp sending-account lifecycle (Phase 6)
    credentials   encrypted credential vault (Phase 6)
    eligibility   pre-queue eligibility + suppression checks
    events        append-only campaign event stream
    inbox         inbound message → conversation foundation (Phase 6)
    phone         strict E.164 phone normalization (Phase 6)
    providers     BaseMarketingProvider + real WhatsApp adapter + MOCK (test)
    queue         DB-backed send queue (idempotency, retry, rate control)
    state         forward-only recipient event state machine (Phase 6)
    suppression   global suppression list + opt-out records
    template      safe {{variable}} template engine
    webhooks      WhatsApp webhook verification + processing (Phase 6)
    worker        worker-side send loop (runs in the worker process)
"""

from app.services.marketing.analytics import AnalyticsService
from app.services.marketing.audience import AudienceService
from app.services.marketing.campaign import CampaignService
from app.services.marketing.channels import CHANNELS, ChannelSpec, get_channel
from app.services.marketing.connections import ConnectionService, resolve_account_credentials
from app.services.marketing.credentials import CredentialVault
from app.services.marketing.eligibility import EligibilityService
from app.services.marketing.events import EventService
from app.services.marketing.inbox import InboxService
from app.services.marketing.phone import PhoneNormalizationService
from app.services.marketing.providers import (
    MarketingProviderRegistry,
    build_provider_registry,
)
from app.services.marketing.queue import QueueService
from app.services.marketing.state import apply_event, can_transition
from app.services.marketing.suppression import SuppressionService
from app.services.marketing.template import TemplateService
from app.services.marketing.webhooks import (
    WhatsAppEventNormalizer,
    WhatsAppWebhookService,
)

__all__ = [
    "AnalyticsService",
    "AudienceService",
    "CHANNELS",
    "CampaignService",
    "ChannelSpec",
    "ConnectionService",
    "CredentialVault",
    "EligibilityService",
    "EventService",
    "InboxService",
    "MarketingProviderRegistry",
    "PhoneNormalizationService",
    "QueueService",
    "SuppressionService",
    "TemplateService",
    "WhatsAppEventNormalizer",
    "WhatsAppWebhookService",
    "apply_event",
    "build_provider_registry",
    "can_transition",
    "get_channel",
    "resolve_account_credentials",
]
