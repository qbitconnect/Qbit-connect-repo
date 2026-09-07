"""SCHEDULED trigger helpers (§11, §57).

Uses the EXISTING worker loop as the only scheduler (no second scheduler
system): the automation worker computes the most recent *slot* for each active
SCHEDULED workflow and fires an internal `schedule.tick` event with a
deterministic per-slot event id. Ticks before `published_at` never fire, and
the unique execution constraint makes re-delivery/restart a no-op — so a
schedule can never double-fire a slot, no matter how often the worker cycles.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.automation.core.exceptions import ConfigurationError

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

VALID_SCHEDULE_TYPES = {"daily", "hourly", "interval", "once"}


def validate_schedule_config(config: dict) -> None:
    schedule_type = config.get("schedule_type")
    if schedule_type not in VALID_SCHEDULE_TYPES:
        raise ConfigurationError(
            f"schedule_type must be one of {sorted(VALID_SCHEDULE_TYPES)}"
        )
    tz_name = config.get("timezone") or "UTC"
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ConfigurationError(f"Unknown timezone: {tz_name!r}") from exc

    if schedule_type == "daily":
        if not config.get("time") or not _TIME_RE.match(str(config["time"])):
            raise ConfigurationError("daily schedule requires time as HH:MM")
    elif schedule_type == "interval":
        minutes = config.get("interval_minutes")
        if not isinstance(minutes, int) or minutes < 1:
            raise ConfigurationError("interval schedule requires interval_minutes >= 1")
    elif schedule_type == "once":
        run_at = config.get("run_at")
        if not run_at:
            raise ConfigurationError("once schedule requires run_at (ISO datetime)")
        try:
            _parse_iso(run_at)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid run_at: {run_at!r}") from exc


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def config_timezone(config: dict) -> ZoneInfo:
    try:
        return ZoneInfo(config.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def current_slot(config: dict, *, now: datetime | None = None) -> datetime | None:
    """The most recent scheduled slot at or before `now` (UTC, aware).

    Returns None when nothing is due yet (e.g. a `once` run_at in the future).
    """
    schedule_type = config.get("schedule_type")
    now = now or datetime.now(timezone.utc)
    tz = config_timezone(config)

    if schedule_type == "once":
        try:
            run_at = _parse_iso(config.get("run_at") or "")
        except ValueError:
            return None
        return run_at if run_at <= now else None

    local_now = now.astimezone(tz)
    if schedule_type == "hourly":
        slot = local_now.replace(minute=0, second=0, microsecond=0)
        return slot.astimezone(timezone.utc)
    if schedule_type == "interval":
        minutes = int(config.get("interval_minutes") or 60)
        epoch = local_now.timestamp()
        slot_ts = (epoch // (minutes * 60)) * (minutes * 60)
        return datetime.fromtimestamp(slot_ts, tz=tz).astimezone(timezone.utc)
    if schedule_type == "daily":
        hh, mm = str(config.get("time") or "00:00").split(":")
        slot = local_now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if slot > local_now:
            slot -= timedelta(days=1)
        return slot.astimezone(timezone.utc)
    return None


def tick_event_id(workflow_id: str, version_id: str, slot: datetime) -> str:
    """Deterministic event id per (workflow, version, slot) — restart-safe
    idempotency for scheduled fires (§11, §13)."""
    return f"schedule:{workflow_id}:{version_id}:{slot.strftime('%Y%m%dT%H%M')}"
