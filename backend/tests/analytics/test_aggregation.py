"""Aggregation worker + diagnostics tests (spec §20, §21, §29)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

from datetime import timedelta

from tests.analytics.conftest import days_ago


class TestIncrementalAggregation:
    async def test_refresh_populates_all_tables(self, app, analytics_data):
        from app.analytics.aggregation import AggregationService

        db = app.state.db
        service = AggregationService(dialect="sqlite")
        async with db.session() as session:
            results = await service.refresh_incremental(session)
        by_table = {r["table"]: r for r in results}
        assert len(results) == 6
        for table in ("analytics_daily_leads", "analytics_daily_scraping",
                      "analytics_daily_campaigns", "analytics_daily_messages",
                      "analytics_daily_conversations", "analytics_daily_automation"):
            assert by_table[table]["status"] == "COMPLETED"
            assert by_table[table]["days"] >= 1

    async def test_leads_aggregates_match_fixture(self, app, analytics_data):
        from app.analytics.aggregation import AggregationService
        from sqlalchemy import select

        from app.models.analytics import AnalyticsDailyLead

        db = app.state.db
        service = AggregationService(dialect="sqlite")
        async with db.session() as session:
            await service.refresh_incremental(session)
            rows = (await session.execute(select(AnalyticsDailyLead))).scalars().all()
        # one row per (day, source); sum over ROWS (a day can hold several)
        assert sum((r.metrics or {}).get("created", 0) for r in rows) == 8
        google_rows = [r for r in rows if r.source == "google_maps"]
        assert sum((r.metrics or {}).get("created", 0) for r in google_rows) == 5

    async def test_second_refresh_is_idempotent(self, app, analytics_data):
        """Re-running must not double-count (upsert idempotency, spec §21)."""
        from app.analytics.aggregation import AggregationService
        from sqlalchemy import select

        from app.models.analytics import AnalyticsDailyLead

        db = app.state.db
        service = AggregationService(dialect="sqlite")
        async with db.session() as session:
            await service.refresh_incremental(session)
            first = len((await session.execute(
                select(AnalyticsDailyLead))).scalars().all())
        async with db.session() as session:
            await service.refresh_incremental(session)
            second = len((await session.execute(
                select(AnalyticsDailyLead))).scalars().all())
        assert first == second
        async with db.session() as session:
            total = sum((r.metrics or {}).get("created", 0) for r in
                        (await session.execute(select(AnalyticsDailyLead))).scalars().all())
        assert total == 8  # never doubled

    async def test_rebuild_range_overwrites(self, app, analytics_data):
        from app.analytics.aggregation import AggregationService
        from sqlalchemy import select

        from app.models.analytics import AnalyticsDailyLead

        db = app.state.db
        service = AggregationService(dialect="sqlite")
        async with db.session() as session:
            await service.refresh_incremental(session)
            await service.rebuild_range(session, days_ago(10).date(),
                                        days_ago(0).date(), domains=["leads"])
            rows = (await session.execute(select(AnalyticsDailyLead))).scalars().all()
        assert sum((r.metrics or {}).get("created", 0) for r in rows) == 8

    async def test_bookkeeping_rows_written(self, app, analytics_data):
        from app.analytics.aggregation import AggregationService

        db = app.state.db
        service = AggregationService(dialect="sqlite")
        async with db.session() as session:
            await service.refresh_incremental(session, triggered_by="WORKER")
            runs = await service.recent_runs(session)
        assert runs
        assert all(r["status"] == "COMPLETED" for r in runs)


class TestDiagnostics:
    async def test_clean_dataset_passes(self, app, analytics_data):
        from app.analytics.aggregation import run_diagnostics

        db = app.state.db
        async with db.session() as session:
            report = await run_diagnostics(session, "sqlite")
        checks = {f["check"]: f["status"] for f in report["findings"]}
        assert checks["negative_aggregate_counts"] == "ok"
        assert checks["recipients_delivered_without_sent"] == "ok"
        assert checks["workflow_executions_orphaned"] == "ok"
        # read-only diagnostics never modify data (spec §29)
        assert "auto-fixed" in report["note"]

    async def test_delivered_without_sent_flagged(self, app, analytics_data):
        from app.analytics.aggregation import run_diagnostics
        from app.models.marketing import CampaignRecipient

        db = app.state.db
        async with db.session() as session:
            # craft an impossible row directly (test-only corruption)
            session.add(CampaignRecipient(
                campaign_id=analytics_data["campaign_id"],
                recipient_address="ghost@example.com", status="DELIVERED",
                delivered_at=days_ago(1), created_at=days_ago(1),
                updated_at=days_ago(1),
            ))
            await session.commit()
            report = await run_diagnostics(session, "sqlite")
        finding = next(f for f in report["findings"]
                       if f["check"] == "recipients_delivered_without_sent")
        assert finding["status"] == "anomaly"
        assert finding["count"] == 1

    async def test_aggregate_mismatch_detection(self, app, analytics_data):
        from app.analytics.aggregation import AggregationService, run_diagnostics

        db = app.state.db
        service = AggregationService(dialect="sqlite")
        async with db.session() as session:
            await service.refresh_incremental(session, domains=["leads"])
            # corrupt one aggregate row (analytics-owned data only)
            from sqlalchemy import select

            from app.models.analytics import AnalyticsDailyLead

            row = (await session.execute(select(AnalyticsDailyLead))).scalars().first()
            row.metrics = {**row.metrics, "created": 999}
            await session.commit()
            report = await run_diagnostics(session, "sqlite")
        assert report["aggregate_mismatch"] is not None
        assert report["aggregate_mismatch"]["aggregate"] != report["aggregate_mismatch"]["live"]
        finding = next(f for f in report["findings"]
                       if f["check"] == "aggregate_leads_matches_live")
        assert finding["status"] == "anomaly"

    async def test_negative_aggregate_flagged(self, app, analytics_data):
        from app.analytics.aggregation import run_diagnostics
        from sqlalchemy import select

        from app.models.analytics import AnalyticsDailyLead

        db = app.state.db
        async with db.session() as session:
            await _seed_minimal_leads_agg(session)
            row = (await session.execute(select(AnalyticsDailyLead))).scalars().first()
            row.metrics = {**row.metrics, "created": -3}
            await session.commit()
            report = await run_diagnostics(session, "sqlite")
        finding = next(f for f in report["findings"]
                       if f["check"] == "negative_aggregate_counts")
        assert finding["status"] == "anomaly"


async def _seed_minimal_leads_agg(session):
    from app.models.analytics import AnalyticsDailyLead

    session.add(AnalyticsDailyLead(day=days_ago(1).date(), source="x",
                                   metrics={"created": 1}))
    await session.flush()


class TestAggregatesAPI:
    async def test_status_and_rebuild_endpoints(self, client, admin_auth,
                                                analytics_data):
        resp = await client.post("/api/v1/analytics/aggregates/rebuild",
                                 json={"domains": ["leads"]}, headers=admin_auth)
        assert resp.status_code == 200
        results = resp.json()["data"]["results"]
        assert results[0]["status"] == "COMPLETED"

        resp = await client.get("/api/v1/analytics/aggregates/status",
                                headers=admin_auth)
        assert resp.status_code == 200
        assert resp.json()["data"]["recent_runs"]

    async def test_rebuild_rejects_inverted_range(self, client, admin_auth):
        resp = await client.post("/api/v1/analytics/aggregates/rebuild", json={
            "day_start": (days_ago(0).date() - timedelta(days=1)).isoformat(),
            "day_end": (days_ago(5).date()).isoformat(),
        }, headers=admin_auth)
        assert resp.status_code == 400
