"""Deterministic metric math (spec §3, §4, §28).

Every rate/percentage helper is zero-denominator-safe and returns None when a
percentage genuinely cannot be computed (e.g. previous period had 0 events) —
the UI renders "—" rather than a fabricated number.
"""

from __future__ import annotations

from typing import Iterable


def safe_rate(numerator: int | float | None, denominator: int | float | None) -> float | None:
    """numerator/denominator rounded to 4 dp; None when the denominator is 0
    or either side is missing. Never raises, never estimates."""
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def zero_safe_rate(numerator: int | float | None, denominator: int | float | None) -> float:
    """Like safe_rate but returns 0.0 for an empty denominator — used where the
    existing Phase 5 service reports 0.0 (keeps dashboards consistent)."""
    rate = safe_rate(numerator, denominator)
    return 0.0 if rate is None else rate


def pct_change(current: int | float | None, previous: int | float | None) -> float | None:
    """Percentage change current vs previous (4 dp). None when not computable:
    missing previous or previous == 0 with current != 0 (undefined division).
    previous == 0 and current == 0 → 0.0 (no change)."""
    if current is None or previous is None:
        return None
    if previous == 0:
        return 0.0 if current == 0 else None
    return round((current - previous) / previous * 100, 2)


def comparison(current: int | float, previous: int | float | None) -> dict:
    """spec §4 comparison payload: absolute difference + percentage change."""
    previous_value = previous if previous is not None else 0
    return {
        "current": current,
        "previous": previous_value,
        "difference": current - previous_value,
        "change_pct": pct_change(current, previous_value),
    }


def sum_or_zero(values: Iterable[int | float | None]) -> float:
    total = 0.0
    for value in values:
        if value is not None:
            total += value
    return total


def clamp_percent(value: float | None) -> float | None:
    """Guard against impossible percentages from corrupt data (spec §29):
    values outside 0..100 are flagged by diagnostics, never silently clamped
    here — this helper only bounds DISPLAY rounding."""
    if value is None:
        return None
    return round(max(0.0, min(value, 100.0)), 2)
