"""Time handling for analytics (spec §23).

Conventions:
- ALL database timestamps are timezone-aware UTC (existing project convention).
- Period boundaries are computed from WALL-CLOCK dates in a user-selected IANA
  timezone, then converted to UTC instants — a report for "September 7" in
  Asia/Kolkata covers exactly September 7 00:00–23:59:59.999999 local time.
- Internally every comparison happens against UTC instants.
- Day-bucket labels for timeseries are produced in SQL (portable per dialect):
  PostgreSQL uses the full timezone database; SQLite (tests/dev) uses a fixed
  offset derived from the timezone at the middle of the queried period, which
  is deterministic for the range being labeled.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Date, func, literal
from sqlalchemy.sql.elements import ColumnElement

from app.analytics.core.exceptions import AnalyticsValidationError

UTC = "UTC"

#: spec §4 period presets
PERIOD_PRESETS = (
    "today", "yesterday", "7d", "30d", "90d", "this_month", "previous_month", "all",
)

_MAX_RANGE_DAYS = 366 * 2  # sanity bound for custom ranges


def resolve_tz(name: str | None) -> ZoneInfo:
    """Resolve an IANA timezone name; invalid names are rejected (never guessed)."""
    candidate = (name or UTC).strip()
    if not candidate:
        candidate = UTC
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AnalyticsValidationError(f"Unknown timezone: {candidate}") from exc


@dataclass(frozen=True)
class Period:
    """A resolved reporting window as UTC instants + display metadata."""

    start: datetime
    end: datetime
    tz: str
    label: str
    #: seconds for "previous equivalent period" (None for 'all')
    length: timedelta | None

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "tz": self.tz,
            "label": self.label,
        }


def _wall(day: date, tz: ZoneInfo, end_of_day: bool = False) -> datetime:
    """Convert a wall-clock date in `tz` to an aware UTC datetime."""
    t = datetime.max.time() if end_of_day else datetime.min.time()
    return datetime.combine(day, t, tzinfo=tz).astimezone(dt_timezone.utc)


def resolve_period(
    *,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tz_name: str | None = None,
    now: datetime | None = None,
) -> Period:
    """Resolve a period preset or custom range to UTC boundaries.

    - presets: today | yesterday | 7d | 30d | 90d | this_month | previous_month | all
    - custom: date_from/date_to as YYYY-MM-DD wall-clock dates in tz
    """
    tzinfo = resolve_tz(tz_name)
    tz_label = str(tzinfo)
    now = now or datetime.now(dt_timezone.utc)
    local_now = now.astimezone(tzinfo)
    today = local_now.date()

    preset = (period or "").strip().lower()
    if preset in ("", "custom"):
        if not date_from and not date_to:
            # default: last 30 days including today
            start = _wall(today - timedelta(days=29), tzinfo)
            return Period(start, now, tz_label, "30d", timedelta(days=30))
        try:
            d_from = date.fromisoformat(date_from) if date_from else None
            d_to = date.fromisoformat(date_to) if date_to else None
        except ValueError as exc:
            raise AnalyticsValidationError("Dates must use YYYY-MM-DD format") from exc
        if d_from is None and d_to is not None:
            d_from = d_to
        if d_to is None and d_from is not None:
            d_to = today if d_from <= today else d_from
        if d_from > d_to:
            raise AnalyticsValidationError("date_from must be on or before date_to")
        if (d_to - d_from).days > _MAX_RANGE_DAYS:
            raise AnalyticsValidationError("Date range exceeds the 2-year maximum")
        start = _wall(d_from, tzinfo)
        end = _wall(d_to, tzinfo, end_of_day=True)
        if end > now:  # future instants contain no data; clamp to now
            end = now
        if end < start:
            end = start
        return Period(start, end, tz_label, f"{d_from.isoformat()}..{d_to.isoformat()}",
                      timedelta(days=(d_to - d_from).days + 1))

    if preset == "today":
        start = _wall(today, tzinfo)
        return Period(start, max(now, start), tz_label, "today", timedelta(days=1))
    if preset == "yesterday":
        start = _wall(today - timedelta(days=1), tzinfo)
        end = _wall(today, tzinfo)
        return Period(start, end, tz_label, "yesterday", timedelta(days=1))
    if preset in ("7d", "30d", "90d"):
        days = int(preset.rstrip("d"))
        start = _wall(today - timedelta(days=days - 1), tzinfo)
        return Period(start, max(now, start), tz_label, preset, timedelta(days=days))
    if preset == "this_month":
        start = _wall(today.replace(day=1), tzinfo)
        return Period(start, max(now, start), tz_label, "this_month", None)
    if preset == "previous_month":
        first_of_month = today.replace(day=1)
        last_month_end = first_of_month - timedelta(days=1)
        start = _wall(last_month_end.replace(day=1), tzinfo)
        end = _wall(last_month_end, tzinfo, end_of_day=True)
        return Period(start, end, tz_label, "previous_month", None)
    if preset == "all":
        return Period(datetime(2000, 1, 1, tzinfo=dt_timezone.utc), now, tz_label, "all", None)

    raise AnalyticsValidationError(
        f"Unknown period preset: {preset!r}. Valid: {', '.join(PERIOD_PRESETS)}"
    )


def previous_period(p: Period) -> Period | None:
    """The equivalent period immediately before `p` (spec §4 comparison).

    - fixed-length and custom periods shift back by their own length
    - calendar periods map semantically (this_month → previous_month)
    - 'all' has no previous window → None (comparison omitted, honestly)
    """
    if p.label == "all":
        return None
    if p.label == "this_month":
        return resolve_period(period="previous_month", tz_name=p.tz)
    # fixed-length, calendar and custom periods: shift back by their own length
    length = p.length or (p.end - p.start)
    return Period(
        start=p.start - length,
        end=p.end - length,
        tz=p.tz,
        label=f"previous {p.label}",
        length=length,
    )


def utc_offset_seconds(instant: datetime, tz_name: str) -> int:
    """UTC offset (seconds) of `tz_name` at a given instant. Used ONLY for the
    SQLite day-bucket dialect; PostgreSQL uses the full timezone database."""
    tzinfo = resolve_tz(tz_name)
    return int(instant.astimezone(tzinfo).utcoffset().total_seconds())


def day_bucket(
    column: ColumnElement, tz_name: str, start: datetime, end: datetime, *,
    dialect: str = "sqlite",
):
    """Portable SQL expression labeling `column` (UTC-aware timestamp) with the
    wall-clock DATE in `tz_name` as a YYYY-MM-DD string.

    - PostgreSQL (dialect="postgresql"): full timezone database conversion
      (`timezone(tz, col)::date`, DST-safe for every row)
    - SQLite (tests/dev): fixed offset at the range midpoint (`date(col, ...)`)
    Both produce identical labels for UTC (offset 0), which is the default.
    """
    tz_name = tz_name or UTC
    if dialect == "postgresql":
        localized = func.timezone(tz_name, column)
        return func.to_char(func.cast(localized, Date), "YYYY-MM-DD")
    offset = utc_offset_seconds(start + (end - start) / 2, tz_name)
    sign = "+" if offset >= 0 else "-"
    modifier = f"{sign}{abs(offset)} seconds"
    return func.date(column, literal(modifier))


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc).isoformat()


def days_between(start: datetime, end: datetime) -> int:
    return max(1, (end - start).days + 1)


def stable_hash(payload: dict) -> str:
    import json

    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]
