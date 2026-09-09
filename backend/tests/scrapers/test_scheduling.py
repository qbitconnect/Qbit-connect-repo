"""Scrape schedule foundation tests (spec §SCHEDULING).

- recurrence math: ONCE / INTERVAL / DAILY (timezone-aware)
- validation: interval floor, HH:MM format, unknown timezone
- claim discipline: due-claim advances next_run_at, single-claim semantics,
  ONCE self-disables, max_runs respected
- outcome tracking: consecutive failures auto-disable at the limit
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

import pytest

from app.models.scrape import ScrapeSchedule
from app.services.scraping.scheduling import (
    MAX_CONSECUTIVE_FAILURES,
    ScrapeScheduleService,
    compute_next_run,
)


async def _mk(session, **overrides) -> ScrapeSchedule:
    now = datetime.now(dt_timezone.utc)
    defaults = dict(
        actor_id="website",
        input={"url": "https://example.com"},
        schedule_type="INTERVAL",
        interval_seconds=3600,
        timezone="UTC",
        enabled=True,
        next_run_at=now - timedelta(minutes=5),
        run_count=0,
        failure_count=0,
        created_by=uuid.uuid4(),
        organization_id=uuid.uuid4(),
    )
    defaults.update(overrides)
    row = ScrapeSchedule(**defaults)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_interval_recurrence(scrape_env):
    settings, db, *_rest = scrape_env
    async with db.session() as session:
        row = await _mk(session, interval_seconds=900)
        after = datetime.now(dt_timezone.utc)
        nxt = compute_next_run(row, after)
        assert nxt is not None
        assert 800 <= (nxt - after).total_seconds() <= 910


@pytest.mark.asyncio
async def test_daily_recurrence_respects_timezone(scrape_env):
    settings, db, *_rest = scrape_env
    async with db.session() as session:
        row = await _mk(
            session, schedule_type="DAILY", daily_time="23:30", timezone="Asia/Kolkata",
            interval_seconds=None,
        )
        after = datetime(2026, 9, 10, 12, 0, tzinfo=dt_timezone.utc)  # 17:30 IST
        nxt = compute_next_run(row, after)
        assert nxt is not None
        # 23:30 IST on the same day == 18:00 UTC
        assert nxt.hour == 18 and nxt.minute == 0
        assert nxt.date() == after.date()


@pytest.mark.asyncio
async def test_once_has_no_recurrence(scrape_env):
    settings, db, *_rest = scrape_env
    async with db.session() as session:
        row = await _mk(session, schedule_type="ONCE", interval_seconds=None)
        assert compute_next_run(row, datetime.now(dt_timezone.utc)) is None


@pytest.mark.asyncio
async def test_validation_errors(scrape_env):
    settings, db, *_rest = scrape_env
    async with db.session() as session:
        service = ScrapeScheduleService(session)
        with pytest.raises(Exception, match="interval_seconds"):
            await service.create(
                actor_id="website", input={"url": "https://x.com"},
                schedule_type="INTERVAL", interval_seconds=10,
            )
        with pytest.raises(Exception, match="daily_time"):
            await service.create(
                actor_id="website", input={"url": "https://x.com"},
                schedule_type="DAILY", daily_time="25:99",
            )
        with pytest.raises(Exception, match="timezone"):
            await service.create(
                actor_id="website", input={"url": "https://x.com"},
                schedule_type="DAILY", daily_time="10:00", timezone_name="Mars/Olympus",
            )
        with pytest.raises(Exception, match="schedule_type"):
            await service.create(
                actor_id="website", input={"url": "https://x.com"},
                schedule_type="CRON",
            )


@pytest.mark.asyncio
async def test_claim_advances_and_once_disables(scrape_env):
    settings, db, *_rest = scrape_env
    async with db.session() as session:
        # INTERVAL: claim advances next_run_at, increments run_count
        row = await _mk(session)
        service = ScrapeScheduleService(session)
        claimed = await service.claim_due(row.id, owner="w1")
        assert claimed is not None
        assert claimed.run_count == 1
        assert claimed.next_run_at > datetime.now(dt_timezone.utc) - timedelta(seconds=5)
        # a second claim before the next due moment loses
        assert await service.claim_due(row.id, owner="w2") is None

        # ONCE: claim disables the schedule (max_runs defaults to 1)
        once = await _mk(session, schedule_type="ONCE", interval_seconds=None, max_runs=None)
        svc = ScrapeScheduleService(session)
        claimed_once = await svc.claim_due(once.id, owner="w1")
        assert claimed_once is not None
        assert claimed_once.enabled is False
        assert claimed_once.next_run_at is None


@pytest.mark.asyncio
async def test_max_runs_stops_claims(scrape_env):
    settings, db, *_rest = scrape_env
    async with db.session() as session:
        row = await _mk(session, run_count=5, max_runs=5)
        service = ScrapeScheduleService(session)
        assert await service.claim_due(row.id, owner="w1") is None


@pytest.mark.asyncio
async def test_consecutive_failures_auto_disable(scrape_env):
    settings, db, *_rest = scrape_env
    async with db.session() as session:
        row = await _mk(session)
        service = ScrapeScheduleService(session)
        for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
            await service.mark_outcome(row.id, ok=False, error="boom")
        await session.refresh(row)
        assert row.enabled is True  # still fighting
        await service.mark_outcome(row.id, ok=False, error="boom")
        await session.refresh(row)
        assert row.enabled is False  # auto-disabled at the limit
        assert "boom" in (row.last_error or "")
        # a success resets the streak
        row.enabled = True
        row.failure_count = 0
        await session.commit()
        await service.mark_outcome(row.id, ok=True)
        await session.refresh(row)
        assert row.failure_count == 0 and row.last_error is None
