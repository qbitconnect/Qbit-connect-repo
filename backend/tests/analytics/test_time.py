"""Time + math primitives (spec §4, §23, §28)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

import pytest

from app.analytics.core.exceptions import AnalyticsValidationError
from app.analytics.core.math import comparison, pct_change, safe_rate
from app.analytics.core.time import (
    Period,
    day_bucket,
    previous_period,
    resolve_period,
    resolve_tz,
)


class TestPeriodResolution:
    def test_7d_spans_seven_wall_clock_days(self):
        period = resolve_period(period="7d")
        assert (period.end - period.start).days == 6  # 7 inclusive days
        assert period.length == timedelta(days=7)

    def test_today_starts_at_local_midnight(self):
        period = resolve_period(period="today", tz_name="UTC")
        assert period.start.utcoffset() == timedelta(0)
        assert period.start.hour == 0 and period.start.minute == 0

    def test_kolkata_date_boundary_differs_from_utc(self):
        # 18:00 UTC on Sep 7 = 23:30 IST Sep 7; 'today' in IST starts 18:30 UTC
        probe = datetime(2026, 9, 7, 18, 0, tzinfo=dt_timezone.utc)
        utc_period = resolve_period(period="today", tz_name="UTC", now=probe)
        ist_period = resolve_period(period="today", tz_name="Asia/Kolkata", now=probe)
        assert utc_period.start.hour == 0
        assert ist_period.start.hour == 18  # IST midnight == 18:30 UTC (on the hour here)
        assert ist_period.start.date().isoformat() == "2026-09-06"

    def test_september_7_report_is_september_7(self):
        """spec §23: a report for September 7 must not drift to Sep 6/8."""
        period = resolve_period(period="custom", date_from="2026-09-07",
                                date_to="2026-09-07", tz_name="Asia/Kolkata")
        local_start = period.start.astimezone(resolve_tz("Asia/Kolkata"))
        local_end = period.end.astimezone(resolve_tz("Asia/Kolkata"))
        assert local_start.date().isoformat() == "2026-09-07"
        assert local_end.date().isoformat() == "2026-09-07"

    def test_past_custom_range_covers_full_last_day(self):
        period = resolve_period(period="custom", date_from="2026-08-01",
                                date_to="2026-08-31", tz_name="UTC")
        assert period.end.hour == 23 and period.end.minute == 59
        assert period.end.second == 59
        assert period.start.hour == 0

    def test_custom_range_validation(self):
        with pytest.raises(AnalyticsValidationError):
            resolve_period(period="custom", date_from="2026-09-10",
                           date_to="2026-09-01")
        with pytest.raises(AnalyticsValidationError):
            resolve_period(period="custom", date_from="not-a-date")

    def test_range_length_cap(self):
        with pytest.raises(AnalyticsValidationError):
            resolve_period(period="custom", date_from="2020-01-01",
                           date_to="2026-09-07")

    def test_unknown_preset_rejected(self):
        with pytest.raises(AnalyticsValidationError):
            resolve_period(period="fortnight")

    def test_unknown_timezone_rejected_not_guessed(self):
        with pytest.raises(AnalyticsValidationError):
            resolve_period(period="7d", tz_name="Mars/Olympus")

    def test_previous_period_fixed_length(self):
        period = resolve_period(period="7d")
        prev = previous_period(period)
        assert prev is not None
        assert prev.end <= period.start
        assert prev.length == period.length

    def test_previous_of_this_month_is_previous_month(self):
        period = resolve_period(period="this_month")
        prev = previous_period(period)
        assert prev.label == "previous_month"

    def test_previous_of_all_is_none(self):
        period = resolve_period(period="all")
        assert previous_period(period) is None


class TestDayBucket:
    def test_utc_buckets_sqlite(self):
        from sqlalchemy import literal_column

        expr = day_bucket(literal_column("some_table.created_at"), "UTC",
                          datetime(2026, 9, 1, tzinfo=dt_timezone.utc),
                          datetime(2026, 9, 7, tzinfo=dt_timezone.utc),
                          dialect="sqlite")
        compiled = str(expr.compile(compile_kwargs={"literal_binds": True}))
        assert "date(" in compiled  # SQLite date() with modifier

    def test_fixed_offset_for_non_utc_on_sqlite(self):
        from sqlalchemy import literal_column

        expr = day_bucket(literal_column("t.created_at"), "Asia/Kolkata",
                          datetime(2026, 9, 1, tzinfo=dt_timezone.utc),
                          datetime(2026, 9, 7, tzinfo=dt_timezone.utc),
                          dialect="sqlite")
        compiled = str(expr.compile(compile_kwargs={"literal_binds": True}))
        assert "19800 seconds" in compiled  # +5:30 offset


class TestMath:
    def test_safe_rate_zero_denominator_is_none(self):
        assert safe_rate(5, 0) is None
        assert safe_rate(5, None) is None
        assert safe_rate(None, 5) is None
        assert safe_rate(1, 4) == 0.25

    def test_pct_change(self):
        assert pct_change(110, 100) == 10.0
        assert pct_change(50, 100) == -50.0
        assert pct_change(10, 0) is None      # undefined, never fabricated
        assert pct_change(0, 0) == 0.0
        assert pct_change(5, None) is None

    def test_comparison_payload(self):
        out = comparison(30, 20)
        assert out == {"current": 30, "previous": 20, "difference": 10,
                       "change_pct": 50.0}

    def test_comparison_zero_previous(self):
        out = comparison(7, 0)
        assert out["difference"] == 7
        assert out["change_pct"] is None


class TestPeriodDataclass:
    def test_period_to_dict_iso(self):
        p = Period(start=datetime(2026, 9, 1, tzinfo=dt_timezone.utc),
                   end=datetime(2026, 9, 7, tzinfo=dt_timezone.utc),
                   tz="UTC", label="7d", length=timedelta(days=7))
        assert p.to_dict()["label"] == "7d"
        assert p.to_dict()["start"].startswith("2026-09-01T00:00:00+00:00")
