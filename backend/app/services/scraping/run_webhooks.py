"""Run-lifecycle webhooks (Actor Platform spec §22).

Events: RUN_CREATED / RUN_STARTED / RUN_SUCCEEDED / RUN_FAILED /
RUN_ABORTED / RUN_TIMED_OUT. Delivery is INSERT-ONLY here: the API/runner
process only records delivery rows in the same transaction as the state
change; the worker loop performs the HTTP POST with:

- HMAC-SHA256 signature header (X-QBIT-Signature: t=<ts>,v1=<hex>)
- 10-second timeout, exponential backoff (5 attempts max)
- honest delivery bookkeeping (status code / error stored per attempt)

No signature forgery protection gaps: the timestamp is part of the signed
payload, so replays with stale timestamps fail verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.actor_platform import RunWebhook, RunWebhookDelivery

logger = get_logger("qbit.scraping.webhooks")

RUN_EVENTS = (
    "RUN_CREATED",
    "RUN_STARTED",
    "RUN_SUCCEEDED",
    "RUN_FAILED",
    "RUN_ABORTED",
    "RUN_TIMED_OUT",
)

#: delivery knobs
DELIVERY_TIMEOUT_SECONDS = 10.0
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 3600.0


def signing_timestamp() -> str:
    return str(int(datetime.now(timezone.utc).timestamp()))


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(timestamp.encode("utf-8"))
    mac.update(b".")
    mac.update(body)
    return mac.hexdigest()


class RunWebhookService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --------------------------------------------------------------- emission
    async def emit(
        self,
        *,
        event: str,
        job_id: uuid.UUID | None = None,
        actor_id: str | None = None,
        payload: dict,
    ) -> int:
        """Record a delivery row per matching webhook (PENDING). Returns the
        number of rows queued. Called INSIDE the caller's transaction — the
        delivery itself is performed by the worker loop, never inline."""
        if event not in RUN_EVENTS:
            raise ValueError(f"Unknown run event: {event}")
        hooks = (
            await self.session.execute(
                select(RunWebhook).where(RunWebhook.enabled.is_(True))
            )
        ).scalars().all()
        queued = 0
        for hook in hooks:
            if hook.actor_id and actor_id and hook.actor_id != actor_id:
                continue
            events = hook.events or []
            if events and event not in events:
                continue
            self.session.add(
                RunWebhookDelivery(
                    webhook_id=hook.id,
                    event=event,
                    job_id=job_id,
                    actor_id=actor_id,
                    payload=payload,
                    status="PENDING",
                    created_at=datetime.now(timezone.utc),
                )
            )
            queued += 1
        if queued:
            await self.session.flush()
        return queued

    async def list(self, *, actor_id: str | None = None):
        stmt = select(RunWebhook).order_by(RunWebhook.created_at.desc())
        if actor_id:
            stmt = stmt.where(RunWebhook.actor_id == actor_id)
        return list((await self.session.execute(stmt)).scalars())

    async def deliveries(self, webhook_id: uuid.UUID, limit: int = 50):
        rows = await self.session.execute(
            select(RunWebhookDelivery)
            .where(RunWebhookDelivery.webhook_id == webhook_id)
            .order_by(RunWebhookDelivery.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars())


async def deliver_pending_webhooks(session_factory, *, max_batch: int = 25) -> int:
    """Worker-loop step: deliver due PENDING webhooks. Returns delivered count.

    Exponential backoff: 30s * 2^(attempt-1), capped at 1h. A delivery is
    DELIVERED only on a 2xx response — everything else is retried honestly.
    """
    now = datetime.now(timezone.utc)
    delivered = 0
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(RunWebhookDelivery)
                .where(RunWebhookDelivery.status == "PENDING")
                .where(
                    (RunWebhookDelivery.next_retry_at.is_(None))
                    | (RunWebhookDelivery.next_retry_at <= now)
                )
                .order_by(RunWebhookDelivery.created_at)
                .limit(max_batch)
            )
        ).scalars().all()
        if not rows:
            return 0
        hook_ids = {row.webhook_id for row in rows}
        hooks = {
            hook.id: hook
            for hook in (
                await session.execute(
                    select(RunWebhook).where(RunWebhook.id.in_(hook_ids))
                )
            ).scalars().all()
        }
        for delivery in rows:
            hook = hooks.get(delivery.webhook_id)
            if hook is None or not hook.enabled:
                delivery.status = "FAILED"
                delivery.last_error = "webhook removed or disabled"
                continue
            body = json.dumps(
                {
                    "event": delivery.event,
                    "actor_id": delivery.actor_id,
                    "job_id": str(delivery.job_id) if delivery.job_id else None,
                    "payload": delivery.payload or {},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                default=str,
            ).encode("utf-8")
            ts = signing_timestamp()
            signature = sign_payload(hook.secret, ts, body)
            ok, status_code, error = await _post(
                hook.url, body, ts, signature
            )
            delivery.attempts += 1
            delivery.last_status_code = status_code
            if ok:
                delivery.status = "DELIVERED"
                delivery.delivered_at = datetime.now(timezone.utc)
                delivered += 1
            else:
                delivery.last_error = (error or "")[:2000]
                if delivery.attempts >= delivery.max_attempts:
                    delivery.status = "FAILED"
                else:
                    delay = min(
                        BACKOFF_BASE_SECONDS * (2 ** (delivery.attempts - 1)),
                        BACKOFF_MAX_SECONDS,
                    )
                    delivery.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        await session.commit()
    return delivered


async def _post(url: str, body: bytes, ts: str, signature: str):
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "QBITConnect-Webhook/1.0",
        "X-QBIT-Timestamp": ts,
        "X-QBIT-Signature": f"t={ts},v1={signature}",
    }
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https"):
        return False, None, f"refusing to deliver over unsupported scheme {scheme!r}"
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, content=body, headers=headers)
        return (200 <= resp.status_code < 300), resp.status_code, None
    except httpx.HTTPError as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
