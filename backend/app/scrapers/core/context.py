"""ScraperContext — the controlled runtime handed to actors (brief §10).

Actors get NOTHING but this object: no raw DB sessions, no filesystem, no
arbitrary settings. It provides:

- identity        job_id, actor_id, actor_version, attempt
- logging         job-scoped structured logger adapter (brief §39)
- network         ctx.http (PolicyHttpClient) — the only network path
- progress        ctx.progress (batched counters, honest percentages, §38)
- events          await ctx.report(type, message, metadata)   (§13)
- checkpoints     ctx.checkpoint_cursor(cursor) / await ctx.save_checkpoint() (§18)
- controls        await ctx.check_stopped() at safe points; ctx.is_cancelled()
- limits          ctx.limits (deadline, max_pages, max_records) + check helpers
- settings        read-only Settings reference for actors that need knobs

Streaming contract (brief §25): `run(ctx)` is an async GENERATOR yielding raw
lead-shaped items; the platform (runner → pipeline) normalizes/dedups/persists
them (doc 08 §2: actors never touch the database).

Pause/cancel are COOPERATIVE (§19): the runner polls the durable control flag
(DB `stop_requested` + queue control key) via an injected async `control_reader`
and translates it: PAUSE → ScraperPausedError at the next safe point, CANCEL →
ScraperCancelledError. Actors that cannot safely pause declare
`supports_pause = False` and the UI communicates it (§4).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.scrapers.core.exceptions import (
    ScraperCancelledError,
    ScraperLimitReachedError,
    ScraperPausedError,
    ScraperTimeoutError,
)
from app.scrapers.core.http import HttpPolicy, PolicyHttpClient
from app.scrapers.core.netguard import UrlPolicy


@dataclass
class JobLimits:
    """Effective resource limits for one job (brief §32)."""

    max_runtime_seconds: float = 3600.0
    max_pages: int | None = None
    max_records: int | None = None
    started_at: float = field(default_factory=time.monotonic)

    def deadline_exceeded(self) -> bool:
        return (time.monotonic() - self.started_at) > self.max_runtime_seconds

    def records_exceeded(self, records: int) -> bool:
        return self.max_records is not None and records >= self.max_records

    def pages_exceeded(self, pages: int) -> bool:
        return self.max_pages is not None and pages >= self.max_pages


class JobLoggerAdapter(logging.LoggerAdapter):
    """Prefixes every record with job/actor correlation (brief §39)."""

    def process(self, msg, kwargs):
        extra = kwargs.get("extra", {})
        fields = extra.setdefault("extra_fields", {})
        fields.setdefault("job_id", str(self.extra.get("job_id")))
        fields.setdefault("actor_id", self.extra.get("actor_id"))
        fields.setdefault("actor_version", self.extra.get("actor_version"))
        return msg, kwargs


class _NullProgress:
    """No-op progress sink — lets actors use ctx.progress unconditionally
    (unit tests construct contexts without a reporter)."""

    records_found = 0
    records_saved = 0
    records_duplicate = 0
    records_failed = 0
    pages_fetched = 0
    stage = ""

    def add_found(self, n: int = 1) -> None: ...

    def add_saved(self, n: int = 1) -> None: ...

    def add_duplicate(self, n: int = 1) -> None: ...

    def add_failed(self, n: int = 1) -> None: ...

    def add_page(self, n: int = 1) -> None: ...

    def set_stage(self, stage: str) -> None: ...

    async def maybe_flush(self, *, force: bool = False) -> None: ...

    async def flush(self) -> None: ...


class ScraperContext:
    """Runtime handed to actor.run(). One instance per job attempt."""

    def __init__(
        self,
        *,
        job_id: uuid.UUID,
        actor_id: str,
        actor_version: str,
        attempt: int = 1,
        input: dict | None = None,
        config: dict | None = None,
        limits: JobLimits | None = None,
        http_policy: HttpPolicy | None = None,
        url_policy: UrlPolicy | None = None,
        progress=None,
        events=None,
        checkpoint=None,
        control_reader: Callable[[], Awaitable[str | None]] | None = None,
        logger: logging.Logger | None = None,
        settings=None,
        http_transport=None,  # test hook (httpx.MockTransport)
    ) -> None:
        self.job_id = job_id
        self.actor_id = actor_id
        self.actor_version = actor_version
        self.attempt = attempt
        self.input = input or {}
        self.config = config or {}
        self.settings = settings
        self.limits = limits or JobLimits()
        self.progress = progress or _NullProgress()
        self._real_progress = progress is not None
        self.events = events
        self.checkpoint = checkpoint
        self._control_reader = control_reader
        self.logger = JobLoggerAdapter(
            logger or logging.getLogger("qbit.scrapers.actor"),
            {"job_id": job_id, "actor_id": actor_id, "actor_version": actor_version},
        )
        self.url_policy = url_policy or UrlPolicy()
        self.http_policy = http_policy or HttpPolicy()
        self._http: PolicyHttpClient | None = None
        self._http_transport = http_transport
        self._stop_reason: str | None = None
        self._pause_requested = False
        self._closed = False

    # ---------------------------------------------------------------- network
    @property
    def http(self) -> PolicyHttpClient:
        if self._http is None:
            self._http = PolicyHttpClient(
                self.http_policy, self.url_policy, transport=self._http_transport
            )
        return self._http

    # ----------------------------------------------------------------- events
    async def report(
        self, event_type: str, message: str | None = None, metadata: dict | None = None
    ) -> None:
        if self.events is not None:
            await self.events.emit(event_type, message, metadata)

    # -------------------------------------------------------------- controls
    def is_cancelled(self) -> bool:
        return self._stop_reason == "CANCEL"

    def is_pause_requested(self) -> bool:
        return self._pause_requested or self._stop_reason == "PAUSE"

    def request_pause(self) -> None:
        self._pause_requested = True

    def clear_pause(self) -> None:
        self._pause_requested = False

    def request_cancel(self) -> None:
        self._stop_reason = "CANCEL"

    async def _poll_control(self) -> None:
        """Refresh local state from the durable control flag (fast path).

        The reader is injected by the runner and reads the queue control key
        (Redis/in-process). Failures are swallowed: the DB `stop_requested`
        column remains the durable source of truth and the safe-point check
        must never crash a job because of a control-plane hiccup.
        """
        if self._control_reader is None or self._stop_reason == "CANCEL":
            return
        try:
            flag = await self._control_reader()
        except Exception:  # noqa: BLE001 - control plane must not kill jobs
            return
        if flag == "CANCEL":
            self._stop_reason = "CANCEL"
        elif flag == "PAUSE":
            self._pause_requested = True

    async def check_stopped(self) -> None:
        """Cooperative stop check for safe points (between pages/batches).

        - CANCEL → ScraperCancelledError (runner finalizes the job)
        - PAUSE  → ScraperPausedError  (runner checkpoints + suspends; resume
                   re-enters run() from the last checkpoint)
        """
        await self._poll_control()
        if self.is_cancelled():
            raise ScraperCancelledError("Job cancellation requested")
        if self._pause_requested:
            raise ScraperPausedError("Job pause requested")

    def check_deadline(self) -> None:
        """Job wall-clock budget exhausted → timeout (runner pauses at cp)."""
        if self.limits.deadline_exceeded():
            raise ScraperTimeoutError(
                f"Job exceeded max runtime of {self.limits.max_runtime_seconds:.0f}s"
            )

    def check_record_limit(self, records_found: int) -> None:
        """max_records config limit reached → clean stop, results kept (§32)."""
        if self.limits.records_exceeded(records_found):
            raise ScraperLimitReachedError(
                f"max_records limit reached ({self.limits.max_records})"
            )

    def check_page_limit(self, pages_fetched: int) -> None:
        """max_pages config limit reached → clean stop, results kept (§32)."""
        if self.limits.pages_exceeded(pages_fetched):
            raise ScraperLimitReachedError(
                f"max_pages limit reached ({self.limits.max_pages})"
            )

    # ------------------------------------------------------------ checkpoints
    def checkpoint_cursor(self, cursor: dict) -> None:
        """Actors merge their resume cursor here (small JSON only, §18)."""
        if self.checkpoint is not None:
            self.checkpoint.update(
                cursor,
                self.progress.records_found if self._real_progress else None,
            )

    async def save_checkpoint(self, cursor: dict | None = None, *, force: bool = False) -> None:
        if self.checkpoint is None:
            return
        if cursor:
            self.checkpoint_cursor(cursor)
        if await self.checkpoint.save(force=force):
            await self.report(
                "CHECKPOINT_CREATED", None, {"records": self.checkpoint.records_processed}
            )

    # ----------------------------------------------------------------- close
    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._http is not None:
            await self._http.aclose()

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.limits.started_at

    def now(self):
        from datetime import datetime, timezone

        return datetime.now(timezone.utc)
