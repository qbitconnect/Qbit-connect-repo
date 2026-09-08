"""Background data jobs (Phase 4 §39): large imports/exports run OUTSIDE the
request cycle, processed by the worker process next to the scrape loop.

DB-first claim (same pattern as the scrape queue): a QUEUED row transitions to
PROCESSING under a guarded UPDATE; a crashed worker's stale lease is swept to
FAILED — honest failure instead of silent stalls. Small operations are executed
inline by the API (size thresholds in Settings) — no job is ever faked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.core.logging import get_logger
from app.models.lead import ExportStatus, ImportBatch, ImportStatus, LeadExportRecord
from app.services.files import FileService
from app.services.leads.exporter import LeadExportService
from app.services.leads.importer import LeadImportService
from app.services.storage import StorageService

logger = get_logger("qbit.leads.jobs")

STALE_LEASE_MINUTES = 30


class DataJobWorker:
    def __init__(self, storage: StorageService, files: FileService, *, owner: str = "data-worker") -> None:
        self.storage = storage
        self.files = files
        self.owner = owner
        self.imports = LeadImportService(storage, files)
        self.exports = LeadExportService(storage, files)

    async def process_pending(self, session) -> int:
        """Claim and run at most one import + one export. Returns jobs run."""
        ran = 0
        await self._sweep_stale(session)

        batch = await self.imports.claim_next(session, self.owner)
        if batch is not None:
            logger.info("Data worker claimed import", extra={"extra_fields": {"batch_id": str(batch.id)}})
            await self.imports.run(session, batch, worker_owner=self.owner)
            ran += 1

        export = await self.exports.claim_next(session, self.owner)
        if export is not None:
            logger.info("Data worker claimed export", extra={"extra_fields": {"export_id": str(export.id)}})
            await self.exports.run(session, export, worker_owner=self.owner)
            ran += 1
        return ran

    async def _sweep_stale(self, session) -> None:
        """Honest failure for worker crashes: stale PROCESSING rows → FAILED."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_LEASE_MINUTES)
        for model, failed, label in (
            (ImportBatch, ImportStatus.FAILED.value, "import"),
            (LeadExportRecord, ExportStatus.FAILED.value, "export"),
        ):
            # ImportBatch has NO `error` column (only error_summary/error_file_id);
            # LeadExportRecord does. Setting a non-existent column raises
            # CompileError("Unconsumed column names: error") and killed the whole
            # sweep every worker cycle — only set it where the column exists.
            values: dict = {"status": failed, "completed_at": datetime.now(timezone.utc)}
            if hasattr(model, "error"):
                values["error"] = f"{label} worker lease expired (crash?)"
            await session.execute(
                update(model)
                .where(
                    model.status.in_(["PROCESSING"]),
                    model.leased_at.isnot(None),
                    model.leased_at < cutoff,
                )
                .values(**values)
            )
        await session.commit()
