"""Marketing engine services (Phase 5).

    audience      lead → recipient resolution + immutable snapshot
    campaign      campaign lifecycle (create/validate/launch/pause/…)
    channels      WHATSAPP / EMAIL / SMS declarations
    eligibility   pre-queue eligibility + suppression checks
    events        append-only campaign event stream
    providers     BaseMarketingProvider + channel interfaces + MOCK (test)
    queue         DB-backed send queue (idempotency, retry, rate control)
    suppression   global suppression list + opt-out records
    template      safe {{variable}} template engine
    worker        worker-side send loop (runs in the worker process)
"""

from app.services.marketing.analytics import AnalyticsService
from app.services.marketing.audience import AudienceService
from app.services.marketing.campaign import CampaignService
from app.services.marketing.channels import CHANNELS, ChannelSpec, get_channel
from app.services.marketing.eligibility import EligibilityService
from app.services.marketing.events import EventService
from app.services.marketing.providers import (
    MarketingProviderRegistry,
    build_provider_registry,
)
from app.services.marketing.queue import QueueService
from app.services.marketing.suppression import SuppressionService
from app.services.marketing.template import TemplateService

__all__ = [
    "AnalyticsService",
    "AudienceService",
    "CHANNELS",
    "CampaignService",
    "ChannelSpec",
    "EligibilityService",
    "EventService",
    "MarketingProviderRegistry",
    "QueueService",
    "SuppressionService",
    "TemplateService",
    "build_provider_registry",
    "get_channel",
]
