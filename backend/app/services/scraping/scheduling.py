"""Scrape schedule service (spec §SCHEDULING, §RECURRING INTELLIGENCE).

Dependency-free recurrence engine:
- ONCE     — single run at next_run_at (max_runs defaults to 1)
- INTERVAL — every N seconds (>= 60)
- DAILY    — at HH:MM in an explicit IANA timezone

The worker process is the ONLY scheduler (same discipline as automation
§57): a periodic loop claims due rows with a guarded UPDATE, creates the
scrape job through the regular JobEngine (DB-first enqueue), then advances
next_run_at. Failed creations increment failure_count; after
MAX_CONSECUTIVE_FAILURES consecutive failures the schedule auto-disables
and records the reason — it never silently stops or silently spams.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update

from app.core.errors import NotFoundError, ValidationError
from app.models.scrape import ScrapeSchedule, ScheduleType

MAX_CONSECUTIVE_FAILURES = 10
MIN_INTERVAL_SECONDS = 60


# ----------------------------------------------------------------- computing


def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValidationError(f"Unknown timezone: {name!r}") from exc
    return name or "UTC"


def validate_daily_time(value: str) -> str:
    parts = str(value or "").strip().split(":")
    if (
        len(parts) != 2
        or not all(p.isdigit() for p in parts)
        or not (0 <= int(parts[0]) <= 23)
        or not (0 <= int(parts[1]) <= 59)
    ):
        raise ValidationError("daily_time must be HH:MM (24h)")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes even for timezone=True columns —
    normalize to UTC-aware before any comparison."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value


def compute_next_run(schedule: ScrapeSchedule, after: datetime) -> datetime | None:
    """Next due moment strictly AFTER `after` (UTC). None = finished."""
    stype = (schedule.schedule_type or "").upper()
    if stype == ScheduleType.ONCE:
        return None
    if stype == ScheduleType.INTERVAL:
        seconds = int(schedule.interval_seconds or 0)
        if seconds < MIN_INTERVAL_SECONDS:
            seconds = MIN_INTERVAL_SECONDS
        return after + timedelta(seconds=seconds)
    if stype == ScheduleType.DAILY:
        hh, mm = (schedule.daily_time or "00:00").split(":")[:2]
        tz_name = schedule.timezone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            tz = ZoneInfo("UTC")
        local = after.astimezone(tz)
        candidate = local.replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0
        )
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.astimezone(dt_timezone.utc)
    return None


# ------------------------------------------------------------------ service


class ScrapeScheduleService:
    """CRUD + due-claim loop support for scrape_schedules."""

    def __init__(self, session, audit=None) -> None:
        self.session = session
        self.audit = audit

    # ------------------------------------------------------------- CRUD
    async def create(
        self,
        *,
        actor_id: str,
        input: dict,
        schedule_type: str,
        config: dict | None = None,
        name: str | None = None,
        interval_seconds: int | None = None,
        daily_time: str | None = None,
        timezone_name: str = "UTC",
        max_runs: int | None = None,
        start_at: datetime | None = None,
        created_by: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
    ) -> ScrapeSchedule:
        stype = str(schedule_type or "").upper()
        if stype not in (ScheduleType.ONCE, ScheduleType.INTERVAL, ScheduleType.DAILY):
            raise ValidationError("schedule_type must be ONCE, INTERVAL or DAILY")
        if not isinstance(input, dict) or not input:
            raise ValidationError("input is required")
        if stype == ScheduleType.INTERVAL:
            if interval_seconds is None or interval_seconds < MIN_INTERVAL_SECONDS:
                raise ValidationError(
                    f"interval_seconds must be >= {MIN_INTERVAL_SECONDS}"
                )
        if stype == ScheduleType.DAILY:
            daily_time = validate_daily_time(daily_time or "")
        tz_name = validate_timezone(timezone_name)

        first_run: datetime | None
        if start_at is not None:
            if start_at.tzinfo is None:
                start_at = start_at.replace(tzinfo=dt_timezone.utc)
            first_run = start_at
        elif stype == ScheduleType.DAILY:
            hh, mm = daily_time.split(":")
            tz = ZoneInfo(tz_name)
            local = datetime.now(tz).replace(
                hour=int(hh), minute=int(mm), second=0, microsecond=0
            )
            if local <= datetime.now(tz):
                local += timedelta(days=1)
            first_run = local.astimezone(dt_timezone.utc)
        else:
            first_run = datetime.now(dt_timezone.utc)

        schedule = ScrapeSchedule(
            actor_id=actor_id.strip(),
            name=(name or "").strip()[:200] or None,
            input=input,
            config=config or {},
            schedule_type=stype,
            interval_seconds=interval_seconds,
            daily_time=daily_time,
            timezone=tz_name,
            enabled=True,
            next_run_at=first_run,
            max_runs=1 if stype == ScheduleType.ONCE and max_runs is None else max_runs,
            created_by=created_by,
            organization_id=organization_id,
        )
        self.session.add(schedule)
        await self.session.commit()
        await self.session.refresh(schedule)
        return schedule

    async def list_schedules(
        self,
        *,
        actor_id: str | None = None,
        organization_id: uuid.UUID | None = None,
        enabled: bool | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ScrapeSchedule], int]:
        query = select(ScrapeSchedule)
        if actor_id:
            query = query.where(ScrapeSchedule.actor_id == actor_id)
        if organization_id is not None:
            query = query.where(
                (ScrapeSchedule.organization_id.is_(None))
                | (ScrapeSchedule.organization_id == organization_id)
            )
        if enabled is not None:
            query = query.where(ScrapeSchedule.enabled.is_(enabled))
        total = await self.session.scalar(
            select(func.count()).select_from(query.subquery())
        )
        rows = await self.session.execute(
            query.order_by(ScrapeSchedule.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def get(self, schedule_id: uuid.UUID) -> ScrapeSchedule:
        row = await self.session.get(ScrapeSchedule, schedule_id)
        if row is None:
            raise NotFoundError("Schedule not found")
        return row

    async def set_enabled(self, schedule_id: uuid.UUID, enabled: bool) -> ScrapeSchedule:
        schedule = await self.get(schedule_id)
        schedule.enabled = enabled
        if enabled and schedule.next_run_at is None and schedule.schedule_type != ScheduleType.ONCE:
            schedule.next_run_at = compute_next_run(
                schedule, datetime.now(dt_timezone.utc)
            ) or datetime.now(dt_timezone.utc)
        schedule.updated_at = datetime.now(dt_timezone.utc)
        await self.session.commit()
        await self.session.refresh(schedule)
        return schedule

    async def delete(self, schedule_id: uuid.UUID) -> None:
        schedule = await self.get(schedule_id)
        await self.session.delete(schedule)
        await self.session.commit()

    # --------------------------------------------------- worker claim loop
    async def due_schedules(self, *, limit: int = 10) -> list[ScrapeSchedule]:
        now = datetime.now(dt_timezone.utc)
        rows = await self.session.execute(
            select(ScrapeSchedule)
            .where(
                ScrapeSchedule.enabled.is_(True),
                ScrapeSchedule.next_run_at.is_not(None),
                ScrapeSchedule.next_run_at <= now,
            )
            .order_by(ScrapeSchedule.next_run_at.asc())
            .limit(limit)
        )
        schedules = list(rows.scalars().all())
        for schedule in schedules:
            schedule.next_run_at = _aware(schedule.next_run_at)
            schedule.last_run_at = _aware(schedule.last_run_at)
        return schedules

    async def claim_due(
        self, schedule_id: uuid.UUID, *, owner: str, now: datetime | None = None
    ) -> ScrapeSchedule | None:
        """Optimistic single-writer claim: advance next_run_at while the row
        is still due+enabled. Losers (another worker got there first) get None."""
        now = now or datetime.now(dt_timezone.utc)
        row = await self.session.execute(
            select(ScrapeSchedule).where(ScrapeSchedule.id == schedule_id)
        )
        schedule = row.scalar_one_or_none()
        if schedule is None or not schedule.enabled or schedule.next_run_at is None:
            return None
        schedule.next_run_at = _aware(schedule.next_run_at)
        if schedule.next_run_at > now:
            return None
        if schedule.max_runs is not None and schedule.run_count >= schedule.max_runs:
            return None
        schedule.last_run_at = now
        schedule.next_run_at = compute_next_run(schedule, now)
        if schedule.next_run_at is not None and schedule.next_run_at.tzinfo is None:
            schedule.next_run_at = schedule.next_run_at.replace(tzinfo=dt_timezone.utc)
        if schedule.schedule_type == ScheduleType.ONCE:
            schedule.enabled = False
        schedule.run_count += 1
        schedule.updated_at = now
        await self.session.commit()
        await self.session.refresh(schedule)
        schedule.next_run_at = _aware(schedule.next_run_at)
        schedule.last_run_at = _aware(schedule.last_run_at)
        return schedule

    async def mark_outcome(
        self, schedule_id: uuid.UUID, *, ok: bool, job_id: uuid.UUID | None = None,
        error: str | None = None,
    ) -> None:
        row = await self.session.execute(
            select(ScrapeSchedule).where(ScrapeSchedule.id == schedule_id)
        )
        schedule = row.scalar_one_or_none()
        if schedule is None:
            return
        if ok:
            schedule.failure_count = 0
            schedule.last_error = None
        else:
            schedule.failure_count = (schedule.failure_count or 0) + 1
            schedule.last_error = (error or "unknown error")[:2000]
            if schedule.failure_count >= MAX_CONSECUTIVE_FAILURES:
                schedule.enabled = False
        if job_id is not None:
            schedule.last_job_id = job_id
        schedule.updated_at = datetime.now(dt_timezone.utc)
        await self.session.commit()
