"""Report exports (spec §18, §25) — through the existing ExportService.

- snapshot rows are exported CSV/XLSX/JSON; the export lands in the EXPORT
  storage category with FileService metadata (secure download via files API)
- a run that overflowed the snapshot already produced its own export file —
  that reference is returned instead of re-rendering
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.analytics import Report, ReportSnapshot
from app.services.export import ExportService
from app.services.files import FileService


class ReportExporter:
    def __init__(self, files: FileService) -> None:
        self.files = files
        self._export = ExportService(files)

    async def export_snapshot(
        self, session: AsyncSession, report: Report, snapshot: ReportSnapshot,
        *, format_name: str, requested_by,
    ):
        if format_name not in ("csv", "xlsx", "json"):
            raise NotFoundError(f"Unsupported export format: {format_name}")

        if snapshot.export_file_id is not None:
            # large-run overflow: the file was produced at run time
            record = await self.files.get(session, snapshot.export_file_id)
            if record is None:
                raise NotFoundError("Export file is no longer available")
            return record

        data = snapshot.data or {}
        rows = list(data.get("rows") or [])
        meta = dict(data.get("meta") or {})
        meta["report_id"] = str(report.id)
        meta["report_name"] = report.name
        meta["domain"] = report.domain
        return await self._export.export(
            session,
            format_name=format_name,
            rows=rows,
            base_name=f"report-{report.name[:40].strip().replace(' ', '-')}",
            created_by=requested_by,
            metadata=meta,
        )
