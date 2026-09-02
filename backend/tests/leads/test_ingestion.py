"""Phase 4 §26: scraper → LeadIngestionService → Lead database integration.

Asserts the Phase 3 pipeline still works after being rerouted through the
centralized ingestion service, and that provenance/statistics stay honest.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.lead import LeadActivity
from app.models.scrape import Lead, ScrapeJob
from app.services.scraping.dedup import Deduplicator
from app.services.scraping.pipeline import ResultPipeline
from app.services.scraping.result_files import JobResultFiles


@pytest.mark.asyncio
async def test_pipeline_produces_leads_with_provenance(seeded_db, tmp_path):
    job_id = uuid.uuid4()
    seeded_db.add(ScrapeJob(
        id=job_id, actor_id="website", actor_version="1.2.0",
        input={"start_url": "http://example.com", "max_pages": 1},
    ))
    await seeded_db.commit()

    files = JobResultFiles(tmp_path / "results", actor_id="website", job_id=str(job_id))
    pipeline = ResultPipeline(
        seeded_db, actor_id="website", actor_version="1.2.0", job_id=job_id,
        source="website:example.com", result_files=files, batch_size=2,
    )
    for i in range(5):
        await pipeline.add({
            "business_name": f"Ingest Biz {i}",
            "email": f"ingest{i}@biz.in",
            "phone": f"+91 98000000{i:02d}",
            "city": "Rajkot",
            "source_url": "http://example.com/listing",
        })
    await pipeline.finish()

    leads = (await seeded_db.scalars(select(Lead))).all()
    assert len(leads) == 5
    for lead in leads:
        assert lead.source == "website:example.com"
        assert lead.source_actor_id == "website"
        assert lead.source_actor_version == "1.2.0"
        assert lead.source_job_id == job_id
        assert lead.source_type == "scraper"
        assert lead.quality_score is not None and lead.quality_score >= 60

    # activity trail with job reference
    activities = (await seeded_db.scalars(select(LeadActivity))).all()
    assert len(activities) == 5
    assert all(a.event_type == "lead_scraped" for a in activities)
    assert all(a.metadata_json.get("job_id") == str(job_id) for a in activities)

    # job statistics answer the spec questions
    total = await seeded_db.scalar(select(func.count()).select_from(Lead).where(Lead.source_job_id == job_id))
    assert total == 5


@pytest.mark.asyncio
async def test_pipeline_dedup_still_updates_on_high_confidence(seeded_db, tmp_path):
    job_id = uuid.uuid4()
    files = JobResultFiles(tmp_path / "results", actor_id="website", job_id=str(job_id))
    pipeline = ResultPipeline(
        seeded_db, actor_id="website", actor_version="1.0.0", job_id=job_id,
        result_files=files, batch_size=10,
    )
    item = {"business_name": "Dup Co", "email": "dup@co.in", "phone": "9876512345", "source_url": "http://x.in/1"}
    await pipeline.add(item)
    await pipeline.add(dict(item, business_name="Dup Co Revisited", source_url="http://x.in/2"))
    summary = await pipeline.finish()

    leads = (await seeded_db.scalars(select(Lead))).all()
    assert len(leads) == 1
    lead = leads[0]
    assert lead.seen_count == 2
    # empty fields get filled on re-seen; non-empty are kept
    assert lead.business_name == "Dup Co"
    # without a live job context, finish() reports honest None stats
    assert summary["saved"] is None and summary["duplicate"] is None


@pytest.mark.asyncio
async def test_pipeline_reports_updated_leads_to_progress(seeded_db, tmp_path):
    """Phase 4 §26: merged re-seens increment the job's records_updated counter."""
    from app.scrapers.core.context import ScraperContext
    from app.services.scraping.progress import ProgressReporter

    async def _noop_flush(counters: dict) -> None:
        return None

    job_id = uuid.uuid4()
    files = JobResultFiles(tmp_path / "results", actor_id="website", job_id=str(job_id))
    ctx = ScraperContext(
        job_id=job_id, actor_id="website", actor_version="1.0.0",
        progress=ProgressReporter(_noop_flush),
    )
    pipeline = ResultPipeline(
        seeded_db, actor_id="website", actor_version="1.0.0", job_id=job_id,
        result_files=files, batch_size=10, ctx=ctx,
    )
    item = {"business_name": "Upd Co", "email": "upd@co.in", "phone": "9876500000", "source_url": "http://x.in/1"}
    await pipeline.add(item)
    await pipeline.add(dict(item, source_url="http://x.in/2"))
    await pipeline.finish()

    assert ctx.progress.records_saved == 1
    assert ctx.progress.records_updated == 1
    assert ctx.progress.records_duplicate == 1
    assert ctx.progress.counters["records_updated"] == 1


@pytest.mark.asyncio
async def test_job_statistics_endpoint_shape(seeded_db):
    """count_for_job / list_for_job still work (used by job detail UI)."""
    from app.services.leads import LeadService

    job_id = uuid.uuid4()
    files = JobResultFiles(tmp_path_results(), actor_id="website", job_id=str(job_id))
    pipeline = ResultPipeline(
        seeded_db, actor_id="website", actor_version="1.0.0", job_id=job_id,
        result_files=files,
    )
    for i in range(3):
        await pipeline.add({"business_name": f"Stat {i}", "email": f"stat{i}@x.in"})
    await pipeline.finish()

    svc = LeadService()
    assert await svc.count_for_job(seeded_db, job_id) == 3
    rows, total = await svc.list_for_job(seeded_db, job_id)
    assert total == 3 and len(rows) == 3


def tmp_path_results() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp()) / "results"
