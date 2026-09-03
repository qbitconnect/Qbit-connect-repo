"""Channel abstraction (Phase 5 §2).

Each supported channel (WHATSAPP / EMAIL / SMS) declares its identity,
capabilities, template requirements and the provider ids that can serve it.
Campaign validation uses these declarations; nothing is hard-coded into
CampaignService.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChannelSpec:
    channel_id: str
    name: str
    #: user-facing capability flags
    capabilities: tuple[str, ...]
    #: template requirements enforced by TemplateService/validation
    template: dict
    #: provider registry ids able to serve this channel
    providers: tuple[str, ...]
    #: recipient address kind
    address_kind: str = "phone"  # phone | email


CHANNELS: dict[str, ChannelSpec] = {
    "WHATSAPP": ChannelSpec(
        channel_id="WHATSAPP",
        name="WhatsApp",
        capabilities=("text_message", "template_message", "delivery_events", "read_events"),
        template={
            "requires_subject": False,
            "max_body_chars": 4096,
            "variables": ["first_name", "last_name", "business_name", "city", "company_name"],
        },
        providers=("whatsapp_cloud", "mock"),
    ),
    "EMAIL": ChannelSpec(
        channel_id="EMAIL",
        name="Email",
        capabilities=("subject", "html_body", "plain_text_body", "delivery_events",
                      "bounce_events", "complaint_events", "unsubscribe_links",
                      "open_tracking", "click_tracking"),
        template={
            "requires_subject": True,
            "max_body_chars": 200_000,
            "variables": ["first_name", "last_name", "business_name", "city",
                          "company_name", "email", "unsubscribe_url"],
        },
        # Phase 7: real SMTP + generic Email-API adapters serve this channel;
        # the mock ids only exist inside isolated test environments
        providers=("smtp", "email_api", "email_mock", "email", "mock"),
        address_kind="email",
    ),
    "SMS": ChannelSpec(
        channel_id="SMS",
        name="SMS",
        capabilities=("text_message", "delivery_events"),
        template={
            "requires_subject": False,
            "max_body_chars": 1600,
            "variables": ["first_name", "business_name", "city"],
        },
        providers=("sms", "mock"),
    ),
}

ALL_CHANNELS = tuple(CHANNELS)


def get_channel(channel_id: str | None) -> ChannelSpec | None:
    return CHANNELS.get((channel_id or "").upper())


def is_known_channel(channel_id: str | None) -> bool:
    return (channel_id or "").upper() in CHANNELS
