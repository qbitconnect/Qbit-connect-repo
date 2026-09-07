"""Business-hours shift for WAIT nodes (§30).

Optional configuration on a WAIT node:
    respect_business_hours: true
    business_hours: {"days": [0,1,2,3,4], "start": "09:00", "end": "18:00",
                     "timezone": "UTC"}          # 0=Monday … 6=Sunday

If enabled, the wake-up time is pushed forward until it lands inside the
configured business window (in the configured timezone — never a hard-coded
country timezone). If the window never opens (misconfiguration), the original
time is kept — the wait can only ever be delayed, never lost.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_DAYS = [0, 1, 2, 3, 4]  # Monday–Friday


def _parse_hhmm(value: Any, default: str) -> tuple[int, int]:
    try:
        hh, mm = str(value if value else default).split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        hh, mm = default.split(":")
        return int(hh), int(mm)


def shift_to_business_hours(wake_at: datetime, config: dict | None) -> datetime:
    """Return the first moment >= wake_at inside the business window."""
    if not config:
        return wake_at
    tz_name = (config or {}).get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        tz = ZoneInfo("UTC")
    days = config.get("days") or _DEFAULT_DAYS
    start_h, start_m = _parse_hhmm(config.get("start"), "09:00")
    end_h, end_m = _parse_hhmm(config.get("end"), "18:00")

    local = wake_at.astimezone(tz)
    for _ in range(8):  # at most one week ahead — then give up and keep original
        if local.weekday() in days:
            window_start = local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            window_end = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
            if window_start <= local <= window_end:
                return wake_at
            if local < window_start:
                return window_start.astimezone(dt_timezone.utc)
            # after window end → fall through to next allowed day
        local = (local + timedelta(days=1)).replace(
            hour=start_h, minute=start_m, second=0, microsecond=0
        )
    return wake_at
