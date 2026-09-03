"""Optional open/click tracking (Phase 7 §29–§31).

Privacy model:
- tracking is per-campaign opt-in (track_opens / track_clicks, default OFF)
- tokens are HMAC-signed with QBIT_SECRET_KEY — internal ids are never raw
- click redirects ONLY allow http/https destinations (open-redirect safe, §30)
- accuracy is honestly limited: clients block/prefetch pixels (§31) — the UI
  labels open/click rates as indicative, not exact.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse

from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.models.marketing import CampaignEvent, EmailTrackingEvent
from app.services.marketing.campaigns import CampaignService

PIXEL_BYTES = b"\x89PNG\r\n\x1a\n" + bytes.fromhex(
    "0000000d49484452000000010000000108060000001f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _encode(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(token: str) -> dict:
    padding = "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(token + padding)
    return json.loads(raw.decode("utf-8"))


def make_open_token(campaign_id: uuid.UUID, recipient_id: uuid.UUID, secret: str) -> str:
    payload = _encode({"c": str(campaign_id), "r": str(recipient_id)})
    return f"{payload}.{_sign(payload, secret)}"


def verify_open_token(token: str, secret: str) -> dict:
    try:
        payload, signature = token.rsplit(".", 1)
    except ValueError as exc:
        raise ValidationError("Malformed tracking token") from exc
    if not hmac.compare_digest(_sign(payload, secret), signature):
        raise ValidationError("Invalid tracking token")
    return _decode(payload)


def make_click_token(campaign_id: uuid.UUID, recipient_id: uuid.UUID, url: str, secret: str) -> str:
    payload = _encode({"c": str(campaign_id), "r": str(recipient_id), "u": url})
    return f"{payload}.{_sign(payload, secret)}"


def verify_click_token(token: str, secret: str) -> dict:
    data = verify_open_token(token, secret)  # same scheme
    return data


def is_safe_destination(url: str) -> bool:
    """Only http/https may be redirected (spec §30). Blocks javascript:,
    data:, vbscript:, file:, protocol-relative tricks and whitespace tricks."""
    if not url or len(url) > 2000:
        return False
    candidate = url.strip().replace("\n", "").replace("\r", "").replace("\t", "")
    if candidate.startswith("//"):
        return False
    parsed = urlparse(candidate)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def rewrite_links(html: str, campaign_id: uuid.UUID, recipient_id: uuid.UUID, *, secret: str) -> str:
    """Wrap http(s) links in the click-tracking redirect. Unsafe links are
    left untouched (they will have been removed by the sanitizer)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if not href or not isinstance(href, str):
            continue
        candidate = href.strip().replace("\n", "").replace("\r", "").replace("\t", "")
        if not is_safe_destination(candidate):
            continue
        token = make_click_token(campaign_id, recipient_id, candidate, secret)
        anchor["href"] = f"/t/click/{quote(token)}"
    return str(soup)


def append_open_pixel(html: str, campaign_id: uuid.UUID, recipient_id: uuid.UUID, *, secret: str) -> str:
    token = make_open_token(campaign_id, recipient_id, secret)
    pixel = f'<img src="/t/open/{token}" width="1" height="1" alt="" style="display:none" />'
    if html.lower().rstrip().endswith("</body>"):
        idx = html.lower().rfind("</body>")
        return html[:idx] + pixel + html[idx:]
    return html + pixel


def decode_destination(encoded_url: str) -> str:
    """Decode ?u= from a click redirect (url-safe base64 of the URL)."""
    padding = "=" * (-len(encoded_url) % 4)
    try:
        return base64.urlsafe_b64decode(unquote(encoded_url) + padding).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("Malformed tracking destination") from exc


def encode_destination(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


class TrackingService:
    """Records OPEN/CLICK events with forward-only recipient advancement."""

    def __init__(self, secret: str) -> None:
        self.secret = secret

    async def record_open(
        self,
        session: AsyncSession,
        *,
        campaign,
        recipient,
        user_agent: str | None,
        campaigns: CampaignService,
    ) -> bool:
        if recipient.opened_at is None:
            recipient.opened_at = datetime.now(timezone.utc)
        session.add(
            EmailTrackingEvent(
                campaign_id=campaign.id,
                recipient_id=recipient.id,
                event_type="OPEN",
                user_agent=(user_agent or "")[:500],
            )
        )
        applied = await campaigns.record_event(
            session, campaign, recipient, "OPENED",
            provider_message_id=recipient.provider_message_id,
        )
        return applied

    async def record_click(
        self,
        session: AsyncSession,
        *,
        campaign,
        recipient,
        url: str,
        user_agent: str | None,
        campaigns: CampaignService,
    ) -> bool:
        if not is_safe_destination(url):
            raise ValidationError("Unsafe click destination")
        if recipient.clicked_at is None:
            recipient.clicked_at = datetime.now(timezone.utc)
            campaign.clicked_count += 1  # counted once per recipient (§34)
        session.add(
            EmailTrackingEvent(
                campaign_id=campaign.id,
                recipient_id=recipient.id,
                event_type="CLICK",
                url=url[:2000],
                user_agent=(user_agent or "")[:500],
            )
        )
        applied = await campaigns.record_event(
            session, campaign, recipient, "CLICKED",
            provider_message_id=recipient.provider_message_id,
            payload={"url": url[:500]},
        )
        return applied


def png_pixel() -> bytes:
    return PIXEL_BYTES
