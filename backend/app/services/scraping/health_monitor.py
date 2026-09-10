"""Actor observability + health monitoring (Actor Platform spec §26, §33).

ActorStats   — per-actor aggregates computed from REAL scrape_jobs rows:
               total/succeeded/failed runs, success rate, average duration,
               items saved, duplicate rate, last success/failure. No synthetic
               numbers anywhere.
HealthMonitor — periodic actor health checks persisted to actor_health_checks
               (HEALTHY/DEGRADED/FAILING/UNKNOWN per spec §26). Two signals:
               1. registry check (actor.health_check() — dependencies)
               2. run-history signal: recent failures degrade the status
               When a live probe cannot run (e.g. no outbound network), the
               row says so honestly (UNKNOWN / detail) — never fake-healthy.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.actor_platform import ActorHealthCheck
from app.models.scrape import JobStatus, ScrapeJob

logger = get_logger("qbit.scraping.health")

#: run-history window for the derived health signal
RECENT_WINDOW_HOURS = 72
RECENT_SAMPLE = 8


class ActorStats:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def per_actor(self, actor_id: str) -> dict:
        # portable form: compute pieces separately (SQLite + PG safe)
        total = (
            await self.session.execute(
                select(func.count(ScrapeJob.id)).where(ScrapeJob.actor_id == actor_id)
            )
        ).scalar_one()
        succeeded = (
            await self.session.execute(
                select(func.count(ScrapeJob.id)).where(
                    ScrapeJob.actor_id == actor_id,
                    ScrapeJob.status == JobStatus.COMPLETED.value,
                )
            )
        ).scalar_one()
        failed = (
            await self.session.execute(
                select(func.count(ScrapeJob.id)).where(
                    ScrapeJob.actor_id == actor_id,
                    ScrapeJob.status == JobStatus.FAILED.value,
                )
            )
        ).scalar_one()
        running = (
            await self.session.execute(
                select(func.count(ScrapeJob.id)).where(
                    ScrapeJob.actor_id == actor_id,
                    ScrapeJob.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.PAUSED.value]),
                )
            )
        ).scalar_one()
        items = (
            await self.session.execute(
                select(func.coalesce(func.sum(ScrapeJob.records_saved), 0)).where(
                    ScrapeJob.actor_id == actor_id,
                    ScrapeJob.status == JobStatus.COMPLETED.value,
                )
            )
        ).scalar_one()
        duplicates = (
            await self.session.execute(
                select(func.coalesce(func.sum(ScrapeJob.records_duplicate), 0)).where(
                    ScrapeJob.actor_id == actor_id
                )
            )
        ).scalar_one()
        found = (
            await self.session.execute(
                select(func.coalesce(func.sum(ScrapeJob.records_found), 0)).where(
                    ScrapeJob.actor_id == actor_id
                )
            )
        ).scalar_one()
        avg_seconds = (
            await self.session.execute(
                select(
                    func.avg(
                        func.extract(
                            "epoch",
                            ScrapeJob.completed_at - ScrapeJob.started_at,
                        )
                    )
                ).where(
                    ScrapeJob.actor_id == actor_id,
                    ScrapeJob.status == JobStatus.COMPLETED.value,
                    ScrapeJob.started_at.is_not(None),
                    ScrapeJob.completed_at.is_not(None),
                )
            )
        ).scalar_one()
        last_success = (
            await self.session.execute(
                select(func.max(ScrapeJob.completed_at)).where(
                    ScrapeJob.actor_id == actor_id,
                    ScrapeJob.status == JobStatus.COMPLETED.value,
                )
            )
        ).scalar_one()
        last_failed = (
            await self.session.execute(
                select(func.max(ScrapeJob.completed_at)).where(
                    ScrapeJob.actor_id == actor_id,
                    ScrapeJob.status == JobStatus.FAILED.value,
                )
            )
        ).scalar_one()

        finished = succeeded + failed
        return {
            "actor_id": actor_id,
            "total_runs": int(total or 0),
            "succeeded": int(succeeded or 0),
            "failed": int(failed or 0),
            "active": int(running or 0),
            "success_rate": round(succeeded / finished, 4) if finished else None,
            "avg_runtime_seconds": round(float(avg_seconds), 2) if avg_seconds is not None else None,
            "items_saved": int(items or 0),
            "duplicate_rate": (
                round(float(duplicates) / float(found), 4) if found else None
            ),
            "last_successful_run": last_success.isoformat() if last_success else None,
            "last_failed_run": last_failed.isoformat() if last_failed else None,
        }

    async def all_actors(self, actor_ids: list[str]) -> dict[str, dict]:
        return {actor_id: await self.per_actor(actor_id) for actor_id in actor_ids}

    # ----------------------------------------------------------- platform view
    async def platform(self) -> dict:
        total = (
            await self.session.execute(select(func.count(ScrapeJob.id)))
        ).scalar_one()
        by_status_rows = await self.session.execute(
            select(ScrapeJob.status, func.count(ScrapeJob.id)).group_by(ScrapeJob.status)
        )
        by_status = {status: int(n) for status, n in by_status_rows.all()}
        items = (
            await self.session.execute(
                select(func.coalesce(func.sum(ScrapeJob.records_saved), 0))
            )
        ).scalar_one()
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        runs_24h = (
            await self.session.execute(
                select(func.count(ScrapeJob.id)).where(ScrapeJob.created_at >= since)
            )
        ).scalar_one()
        return {
            "total_runs": int(total or 0),
            "by_status": by_status,
            "items_saved_total": int(items or 0),
            "runs_last_24h": int(runs_24h or 0),
        }


class HealthMonitor:
    """Persists per-actor health rows (spec §26)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        actor_id: str,
        status: str,
        detail: str | None,
        dependencies: dict | None = None,
        duration_ms: int = 0,
        check_kind: str = "registry",
    ) -> ActorHealthCheck:
        row = ActorHealthCheck(
            actor_id=actor_id,
            status=status,
            detail=(detail or "")[:2000],
            dependencies=dependencies or {},
            duration_ms=max(0, int(duration_ms)),
            check_kind=check_kind,
            checked_at=datetime.now(timezone.utc),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def run_history_signal(self, actor_id: str) -> tuple[str, str | None]:
        """Derived status from recent runs: any terminal failures in the last
        N runs → DEGRADED; all-failed → FAILING; no data → UNKNOWN."""
        since = datetime.now(timezone.utc) - timedelta(hours=RECENT_WINDOW_HOURS)
        rows = (
            await self.session.execute(
                select(ScrapeJob.status)
                .where(
                    ScrapeJob.actor_id == actor_id,
                    ScrapeJob.created_at >= since,
                    ScrapeJob.status.in_(
                        [
                            JobStatus.COMPLETED.value,
                            JobStatus.FAILED.value,
                        ]
                    ),
                )
                .order_by(ScrapeJob.created_at.desc())
                .limit(RECENT_SAMPLE)
            )
        ).scalars().all()
        if not rows:
            return "UNKNOWN", "no finished runs in the last 72h"
        failures = sum(1 for s in rows if s == JobStatus.FAILED.value)
        if failures and failures == len(rows):
            return "FAILING", f"last {len(rows)} runs all failed (72h window)"
        if failures:
            return "DEGRADED", f"{failures} of last {len(rows)} runs failed (72h window)"
        return "HEALTHY", f"last {len(rows)} runs succeeded (72h window)"

    async def check_actor(self, actor) -> ActorHealthCheck:
        """Full check for one actor: registry check + run-history signal,
        combined honestly (worst of the two signals wins)."""
        started = time.monotonic()
        detail_parts: list[str] = []
        try:
            health = await actor.health_check()
            deps = health.dependencies or {}
            if health.status.value in ("READY", "VALIDATED", "REGISTERED"):
                registry_status = "HEALTHY"
                if health.detail:
                    detail_parts.append(health.detail)
            elif health.status.value == "DEGRADED":
                registry_status = "DEGRADED"
                detail_parts.append(health.detail or "actor reports DEGRADED")
            else:
                registry_status = "FAILING"
                detail_parts.append(health.detail or f"actor reports {health.status.value}")
        except Exception as exc:  # noqa: BLE001 — health must never raise
            deps = {}
            registry_status = "FAILING"
            detail_parts.append(f"health_check raised {type(exc).__name__}")
        run_status, run_detail = await self.run_history_signal(actor.id)
        detail_parts.append(run_detail or f"runs: {run_status}")
        worst = _worst_status(registry_status, run_status)
        duration_ms = int((time.monotonic() - started) * 1000)
        return await self.record(
            actor_id=actor.id,
            status=worst,
            detail="; ".join(p for p in detail_parts if p),
            dependencies=deps,
            duration_ms=duration_ms,
            check_kind="registry+history",
        )

    async def latest(self, actor_id: str) -> ActorHealthCheck | None:
        row = (
            await self.session.execute(
                select(ActorHealthCheck)
                .where(ActorHealthCheck.actor_id == actor_id)
                .order_by(ActorHealthCheck.checked_at.desc())
                .limit(1)
            )
        ).scalars().first()
        return row

    async def history(self, actor_id: str, limit: int = 20) -> list[ActorHealthCheck]:
        rows = await self.session.execute(
            select(ActorHealthCheck)
            .where(ActorHealthCheck.actor_id == actor_id)
            .order_by(ActorHealthCheck.checked_at.desc())
            .limit(limit)
        )
        return list(rows.scalars())


_STATUS_ORDER = {"HEALTHY": 0, "UNKNOWN": 1, "DEGRADED": 2, "FAILING": 3}


def _worst_status(a: str, b: str) -> str:
    return a if _STATUS_ORDER.get(a, 0) >= _STATUS_ORDER.get(b, 0) else b
