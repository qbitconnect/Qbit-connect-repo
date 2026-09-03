"""Public email endpoints (Phase 7 §13, §29, §30).

    GET /unsubscribe/{token}                       one-click opt-out (NO login)
    GET /api/v1/email/track/open/{key}?s=<sig>     1x1 pixel (campaign opt-in)
    GET /api/v1/email/track/click/{key}?u=<b64>&s=<sig>   signed redirect

Unsubscribe (§13, §14, §51):
- public by design — a legitimate opt-out request must never require login
- abuse-protected: per-IP rate limit + unguessable token + hash-at-rest
- resolves → persists suppression (channel EMAIL, reason UNSUBSCRIBED) →
  renders an honest confirmation page; reuse is idempotent

Tracking (§29, §30, §31):
- both endpoints exist only for campaigns that enabled them; a disabled
  campaign's keys still resolve (so old emails keep working) but the
  recipient timestamps/rows only move when the campaign opted in — the
  endpoints themselves are always safe, the pixel is 1x1, and the redirect
  re-validates the signature and the http/https scheme (no open redirects,
  no javascript:, no data:).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import Settings
from app.core.logging import get_logger
from app.services.marketing.email_compose import EmailComposer, safe_destination_url
from app.services.marketing.email_tracking import EmailTrackingService
from app.services.marketing.unsubscribe import UnsubscribeService

logger = get_logger("qbit.marketing.email_public")

#: separate routers so the unsubscribe page lives at the site root while
#: tracking endpoints live under /api/v1/email (both PUBLIC — no JWT)
router = APIRouter(tags=["email-public"])
tracking_router = APIRouter(prefix="/api/v1/email", tags=["email-tracking"])

#: 1x1 transparent GIF — the classic tracking pixel response
_PIXEL_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000"
    "010001000002024401003b"
)

_UNSUB_RATE_LIMIT_PER_MIN = 10


def _confirm_page(address_masked: str) -> HTMLResponse:
    """Honest confirmation page (§13 flow last step). No marketing fluff."""
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unsubscribed — QBIT Connect</title></head>
<body style="font-family:system-ui,sans-serif;background:#0f1420;color:#e8eaf0;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0">
<div style="max-width:480px;padding:40px;text-align:center">
<h1 style="font-size:22px;margin-bottom:12px">You're unsubscribed</h1>
<p style="line-height:1.6;color:#a8b0c0">
<strong>{address_masked}</strong> has been removed from this sender's
email marketing list. The opt-out was recorded immediately and applies to
all future campaigns on this channel.</p>
<p style="line-height:1.6;color:#a8b0c0">If you unsubscribe by mistake and
want emails again, you must explicitly re-subscribe through the sender's
own signup process — we never re-enable addresses automatically.</p>
</div></body></html>"""
    return HTMLResponse(content=html, status_code=200)


def _rate_limited(request: Request) -> bool:
    """Abuse protection for the public unsubscribe page (§48).
    Uses the process-local sliding-window limiter (same foundation as login)."""
    limiter = getattr(request.app.state, "unsubscribe_limiter", None)
    if limiter is None:
        return False
    ip = request.client.host if request.client else "unknown"
    return not limiter.check(f"unsubscribe:{ip}")


# ------------------------------------------------------------------ unsubscribe
@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_page(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """§13 flow: token → resolve recipient → confirm opt-out → persist
    suppression → confirmation page. No login. Idempotent on reuse."""
    settings: Settings = request.app.state.settings
    if _rate_limited(request):
        return HTMLResponse(
            "<!DOCTYPE html><html><body><h1>Too many requests</h1>"
            "<p>Please try again in a minute.</p></body></html>", status_code=429,
        )
    service = UnsubscribeService()
    row = await service.resolve(session, raw_token=token)
    if row is None:
        return HTMLResponse(
            "<!DOCTYPE html><html><body style=\"font-family:system-ui;padding:40px\">"
            "<h1>Link not valid</h1><p>This unsubscribe link is unknown, expired "
            "or already invalid. If you keep receiving email, contact the sender "
            "directly.</p></body></html>", status_code=404,
        )
    await service.confirm_opt_out(session, row, source="unsubscribe_link")
    return _confirm_page(row.to_public_dict()["address_masked"])


# ------------------------------------------------------------------ open pixel
@tracking_router.get("/track/open/{tracking_key}")
async def track_open(
    tracking_key: str,
    request: Request,
    s: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
):
    """§29 open-tracking pixel: map key → campaign/recipient/message.
    Signature-verified; forged pixels are answered with a blank GIF anyway —
    never an error body that leaks state."""
    settings: Settings = request.app.state.settings
    from app.services.marketing.email_compose import verify_tracking_signature

    valid = verify_tracking_signature(settings.QBIT_SECRET_KEY, s or "", "open", tracking_key)
    recipient = None
    if valid:
        recipient = await EmailTrackingService().recipient_by_tracking_key(
            session, tracking_key,
        )
    if valid and recipient is not None:
        try:
            await EmailTrackingService().record_open(
                session, recipient=recipient,
                user_agent=request.headers.get("user-agent"),
            )
        except Exception:  # noqa: BLE001 — pixel never errors visibly
            logger.exception("Open tracking record failed")
    return Response(content=_PIXEL_GIF, media_type="image/gif", status_code=200)


# ---------------------------------------------------------------- click click
@tracking_router.get("/track/click/{tracking_key}")
async def track_click(
    tracking_key: str,
    request: Request,
    u: str | None = Query(default=None),
    s: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
):
    """§30 signed click redirect: verify signature → re-validate scheme →
    record → 302 to destination. Forged/unsafe URLs never redirect."""
    settings: Settings = request.app.state.settings
    composer = EmailComposer(
        secret_key=settings.QBIT_SECRET_KEY,
        unsubscribe_base_url=settings.QBIT_EMAIL_UNSUBSCRIBE_BASE_URL,
    )
    destination = composer.resolve_click(
        tracking_key=tracking_key, encoded=u or "", signature=s or "",
    )
    if destination is None:
        raise _invalid_click()
    # defensive double-check (§30: only http/https, never javascript:/data:)
    if safe_destination_url(destination) is None:
        raise _invalid_click()
    recipient = await EmailTrackingService().recipient_by_tracking_key(
        session, tracking_key,
    )
    if recipient is not None:
        try:
            await EmailTrackingService().record_click(
                session, recipient=recipient, url=destination,
                user_agent=request.headers.get("user-agent"),
            )
        except Exception:  # noqa: BLE001 — redirect must survive recording bugs
            logger.exception("Click tracking record failed")
    return RedirectResponse(url=destination, status_code=302)


def _invalid_click():
    from app.core.errors import QBITError

    class _InvalidClick(QBITError):
        status_code = 400
        code = "INVALID_TRACKING_URL"
        message = "Invalid tracking URL"

    return _InvalidClick()
