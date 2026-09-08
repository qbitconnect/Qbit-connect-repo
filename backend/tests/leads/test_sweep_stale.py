"""Regression (found while running the stack locally): DataJobWorker._sweep_stale
set an `error` value on BOTH ImportBatch and LeadExportRecord — but ImportBatch
has no `error` column (only error_summary/error_file_id), so the guarded UPDATE
raised CompileError("Unconsumed column names: error") every worker cycle and the
stale-lease sweep never ran. The sweep must succeed for both models."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.lead import ExportStatus, ImportBatch, ImportStatus, LeadExportRecord
from app.services.leads.jobs import DataJobWorker

STALE = datetime.now(timezone.utc) - timedelta(hours=1)
NOW = datetime.now(timezone.utc)


@pytest.fixture
def worker(app) -> DataJobWorker:
    """Real DataJobWorker wired from the shared app fixture's services."""
    return DataJobWorker(app.state.storage, app.state.files)


@pytest.mark.asyncio
async def test_sweep_marks_stale_processing_rows_failed(seeded_db, worker) -> None:
    """Stale PROCESSING rows on BOTH models must flip to FAILED without raising."""
    batch = ImportBatch(filename="stale.csv", format="csv", status="PROCESSING",
                        leased_at=STALE)
    export = LeadExportRecord(format="csv", status="PROCESSING", leased_at=STALE)
    seeded_db.add_all([batch, export])
    await seeded_db.commit()

    # This used to raise CompileError for ImportBatch (no `error` column).
    await worker._sweep_stale(seeded_db)

    await seeded_db.refresh(batch)
    await seeded_db.refresh(export)
    assert batch.status == ImportStatus.FAILED.value
    assert batch.completed_at is not None
    assert export.status == ExportStatus.FAILED.value
    assert export.completed_at is not None
    assert export.error and "export worker lease expired" in export.error


@pytest.mark.asyncio
async def test_sweep_leaves_fresh_lease_and_queued_rows_alone(seeded_db, worker) -> None:
    """Only rows with an EXPIRED lease are failed; fresh leases / queued stay."""
    fresh = ImportBatch(filename="fresh.csv", format="csv", status="PROCESSING",
                        leased_at=NOW)
    queued = LeadExportRecord(format="csv", status=ExportStatus.QUEUED.value)
    failed_already = LeadExportRecord(format="csv", status=ExportStatus.FAILED.value,
                                      leased_at=STALE, error="original failure")
    seeded_db.add_all([fresh, queued, failed_already])
    await seeded_db.commit()

    await worker._sweep_stale(seeded_db)

    await seeded_db.refresh(fresh)
    await seeded_db.refresh(queued)
    await seeded_db.refresh(failed_already)
    assert fresh.status == "PROCESSING"
    assert queued.status == ExportStatus.QUEUED.value
    # already-failed rows are never rewritten (no clobber of the original error)
    assert failed_already.error == "original failure"
