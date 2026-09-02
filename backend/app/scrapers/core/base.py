"""ScraperActor — the plugin contract (brief §3, §4, §7).

Every actor is an independent package under `scrapers/actors/<slug>/` exposing
one subclass of `ScraperActor`. The core engine and registry contain NO
source-specific logic.

Lifecycle (brief §4):
    DISCOVERED → REGISTERED → VALIDATED → READY → RUNNING → COMPLETED
    failure paths: READY→FAILED, RUNNING→FAILED, RUNNING→PAUSED,
                   PAUSED→RUNNING, RUNNING→CANCELLED

Actor REGISTRATION states (registry) : DISCOVERED/REGISTERED/VALIDATED/READY/
                                       DEGRADED/FAILED/DISABLED — no fake states.
Job RUNTIME states (job engine)      : QUEUED/RUNNING/PAUSED/COMPLETED/FAILED/
                                       CANCELLED (architecture doc 09).

Pause/stop are COOPERATIVE: actors check `ctx.wait_if_paused()` /
`ctx.raise_if_stopped()` at safe points (between pages/batches). Actors that
cannot safely pause set `supports_pause = False` and the UI communicates it.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, ClassVar

from pydantic import BaseModel, ValidationError as PydanticValidationError

from app.scrapers.core.exceptions import ScraperValidationError


class ActorCategory(str, enum.Enum):
    BUSINESS_LEADS = "business_leads"
    WEBSITE = "website"
    DIRECTORY = "directory"
    PUBLIC_DATA = "public_data"
    UNIVERSAL = "universal"
    EMAIL = "email"


class ActorStatus(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    REGISTERED = "REGISTERED"
    VALIDATED = "VALIDATED"
    READY = "READY"
    DEGRADED = "DEGRADED"  # registered, but a dependency is unavailable (brief §49)
    FAILED = "FAILED"      # failed its own health/validation check
    DISABLED = "DISABLED"  # switched off via feature flags (brief §50)


class ActorHealth(BaseModel):
    status: ActorStatus
    detail: str | None = None
    dependencies: dict[str, str] = {}


class ValidationReport(BaseModel):
    valid: bool
    errors: dict[str, str] = {}
    normalized_input: dict = {}


class ScraperActor(ABC):
    """Base contract. Subclasses declare class metadata + implement run()."""

    #: stable slug, e.g. "website" — used in API paths and job rows
    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = ""
    category: ClassVar[ActorCategory] = ActorCategory.UNIVERSAL
    author: ClassVar[str] = "QBIT"
    #: feature strings for the UI cards (brief §7 "Capabilities")
    capabilities: ClassVar[tuple[str, ...]] = ()
    #: whether cooperative pause is technically safe for this actor
    supports_pause: ClassVar[bool] = True
    #: pydantic model defining the strict input schema (brief §8)
    input_schema: ClassVar[type[BaseModel]]
    #: human-readable output field list (brief §9) — normalized lead fields
    output_fields: ClassVar[tuple[str, ...]] = ()

    # ------------------------------------------------------------------ input
    def validate_input(self, data: dict) -> ValidationReport:
        """Strict validation BEFORE a job is created (brief §8: invalid input
        must never reach the worker)."""
        try:
            model = self.input_schema.model_validate(data)
        except PydanticValidationError as exc:
            errors = {}
            for err in exc.errors():
                key = ".".join(str(loc) for loc in err.get("loc", ()) or ("<input>",))
                errors.setdefault(key, err.get("msg", "invalid value"))
            return ValidationReport(valid=False, errors=errors)
        extra = self.validate_policy(model)
        if extra:
            return ValidationReport(valid=False, errors=extra)
        # mode="json" → JSON-safe values (HttpUrl etc.) storable in job rows
        return ValidationReport(
            valid=True, normalized_input=model.model_dump(mode="json", exclude_none=True)
        )

    def validate_policy(self, model: BaseModel) -> dict[str, str]:
        """Hook for actor-specific policy validation beyond the schema.

        Return a mapping of field → error message when the input violates
        source rules (e.g. unsupported region); empty dict means OK.
        """
        return {}

    # ---------------------------------------------------------------- runtime
    @abstractmethod
    def run(self, ctx) -> AsyncIterator[dict]:
        """Async generator yielding RAW lead-shaped dicts (brief §9).

        Streaming contract (brief §25): yield per item; never build the full
        result list in memory. The platform persists: yielded items flow
        through normalization → dedup → lead storage (doc 08 §2 — actors
        never touch the database).

        Must call `await ctx.check_stopped()` at safe points (between pages /
        batches) so pause/cancel are honored cooperatively, and maintain its
        resume cursor via `ctx.checkpoint_cursor(...)` / `ctx.save_checkpoint()`
        so a paused or crashed job resumes from its last checkpoint (§18).
        """

    async def initialize(self, ctx) -> None:  # noqa: ARG002 - hook
        """Optional setup before run()."""

    async def cleanup(self, ctx) -> None:  # noqa: ARG002 - hook
        """Optional teardown after run()/failure. Must never raise."""

    # -------------------------------------------------------------- controls
    async def pause(self, ctx) -> None:
        """Cooperative pause request. The job suspends at its next safe point
        (`ctx.check_stopped()`), the runner checkpoints + sets PAUSED."""
        ctx.request_pause()

    async def resume(self, ctx) -> None:  # noqa: ARG002 - hook
        """Default resume is a no-op: a paused job re-enters run() from its
        checkpoint cursor with the same actor version."""

    async def stop(self, ctx) -> None:
        """Cooperative stop request (cancellation is checkpointed, not forced)."""
        ctx.request_cancel()

    # ----------------------------------------------------------------- health
    async def health_check(self) -> ActorHealth:
        """READY unless a subclass dependency check says otherwise (brief §49)."""
        return ActorHealth(status=ActorStatus.READY)

    # ------------------------------------------------------------- metadata
    def metadata(self) -> dict[str, Any]:
        """Registry metadata exposed to the API/UI (brief §7)."""
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.id,
            "version": self.version,
            "description": self.description,
            "category": self.category.value,
            "author": self.author,
            "capabilities": list(self.capabilities),
            "supports_pause": self.supports_pause,
            "input_fields": self.input_schema.model_json_schema()["properties"],
            "input_schema": self.input_schema.model_json_schema(),
            "output_fields": list(self.output_fields),
        }
