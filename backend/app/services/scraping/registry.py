"""ScraperRegistry — discovery/registration/health of actors (brief §5).

The registry is UI-independent and API-independent. Actors self-register via
`scrapers.bootstrap.register_builtin_actors(registry)` at startup; future
actors can be added by appending one import + one register call (or an
entry-point loader later) without touching the engine.

Actor lifecycle states here: DISCOVERED → REGISTERED → VALIDATED → READY
(with DEGRADED/FAILED/DISABLED side states). No fake states (brief §4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.scrapers.core.base import ActorHealth, ActorStatus, ScraperActor
from app.scrapers.core.exceptions import ScraperError

logger = get_logger("qbit.scrapers.registry")


@dataclass
class RegisteredActor:
    actor: ScraperActor
    status: ActorStatus = ActorStatus.REGISTERED
    detail: str | None = None
    dependencies: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    @property
    def public_status(self) -> ActorStatus:
        if not self.enabled:
            return ActorStatus.DISABLED
        return self.status


class ActorRegistry:
    def __init__(self) -> None:
        self._actors: dict[str, RegisteredActor] = {}

    # ------------------------------------------------------------ lifecycle
    def register(self, actor: ScraperActor, *, enabled: bool = True) -> RegisteredActor:
        if not actor.id or not actor.id.strip():
            raise ScraperError("Actor id must be a non-empty slug")
        if actor.id in self._actors:
            raise ScraperError(f"Actor already registered: {actor.id}")
        self._validate_contract(actor)
        entry = RegisteredActor(actor=actor, status=ActorStatus.REGISTERED, enabled=enabled)
        self._actors[actor.id] = entry
        logger.info(
            "Actor registered",
            extra={"extra_fields": {"actor": actor.id, "version": actor.version}},
        )
        return entry

    def unregister(self, actor_id: str) -> bool:
        return self._actors.pop(actor_id, None) is not None

    def _validate_contract(self, actor: ScraperActor) -> None:
        """VALIDATED state: contract completeness checks (no magic)."""
        problems = []
        if not actor.name:
            problems.append("name is required")
        if not actor.version:
            problems.append("version is required")
        if not actor.description:
            problems.append("description is required")
        if not hasattr(actor, "input_schema") or actor.input_schema is None:
            problems.append("input_schema is required")
        else:
            try:
                actor.input_schema.model_json_schema()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"input_schema invalid: {type(exc).__name__}")
        if not callable(getattr(actor, "run", None)):
            problems.append("run(ctx) is required")
        if problems:
            raise ScraperError(f"Actor {actor.id or '<unnamed>'} failed contract validation: {'; '.join(problems)}")

    # -------------------------------------------------------------- retrieval
    def get(self, actor_id: str) -> ScraperActor:
        entry = self._actors.get(actor_id)
        if entry is None:
            raise KeyError(actor_id)
        return entry.actor

    def entry(self, actor_id: str) -> RegisteredActor | None:
        return self._actors.get(actor_id)

    def list(self, *, include_disabled: bool = True) -> list[RegisteredActor]:
        return [
            entry
            for entry in self._actors.values()
            if include_disabled or entry.enabled
        ]

    def discover(self) -> list[str]:
        """Ids of all discovered actors (registration-state introspection)."""
        return sorted(self._actors)

    def version_of(self, actor_id: str) -> str:
        return self.get(actor_id).version

    # ----------------------------------------------------------------- health
    async def health_check(self, actor_id: str | None = None) -> dict[str, ActorHealth]:
        targets = (
            [self._actors[actor_id]] if actor_id and actor_id in self._actors
            else list(self._actors.values())
        )
        report: dict[str, ActorHealth] = {}
        for entry in targets:
            if not entry.enabled:
                report[entry.actor.id] = ActorHealth(
                    status=ActorStatus.DISABLED, detail="Disabled by configuration"
                )
                continue
            try:
                health = await entry.actor.health_check()
            except Exception as exc:  # noqa: BLE001 - health must never raise
                health = ActorHealth(status=ActorStatus.FAILED, detail=f"{type(exc).__name__}")
            entry.status = health.status
            entry.detail = health.detail
            entry.dependencies = health.dependencies
            report[entry.actor.id] = health
        return report

    def summary(self) -> dict:
        statuses = [e.public_status for e in self._actors.values()]
        return {
            "total": len(self._actors),
            "ready": statuses.count(ActorStatus.READY),
            "degraded": statuses.count(ActorStatus.DEGRADED),
            "disabled": statuses.count(ActorStatus.DISABLED),
            "failed": statuses.count(ActorStatus.FAILED),
        }
