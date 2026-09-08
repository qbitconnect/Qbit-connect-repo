"""ExportService — infrastructure only in Phase 2 (Brief §6).

Format renderers (CSV / JSON / XLSX) behind one interface; generation writes
into the EXPORT storage category and registers metadata via FileService.
Bulk/end-user export endpoints arrive with later phases.
"""

from __future__ import annotations

import csv
import io
import json
from abc import ABC, abstractmethod
from typing import BinaryIO, Iterable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.file import FileCategory
from app.services.files import FileService

logger = get_logger("qbit.export")


class Exporter(ABC):
    format_name: str = "abstract"
    mime_type: str = "application/octet-stream"

    @abstractmethod
    def write(self, rows: Sequence[dict], out: BinaryIO) -> int:
        """Write `rows` to `out`; return row count."""


class CSVExporter(Exporter):
    format_name = "csv"
    mime_type = "text/csv"

    def write(self, rows: Sequence[dict], out: BinaryIO) -> int:
        if not rows:
            return 0
        fieldnames = list(rows[0].keys())
        text_buffer = io.StringIO()
        writer = csv.DictWriter(text_buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        count = 0
        for row in rows:
            writer.writerow(row)
            count += 1
        out.write(text_buffer.getvalue().encode("utf-8"))
        return count


class JSONExporter(Exporter):
    format_name = "json"
    mime_type = "application/json"

    def write(self, rows: Sequence[dict], out: BinaryIO) -> int:
        payload = json.dumps(list(rows), default=str, ensure_ascii=False, indent=2)
        out.write(payload.encode("utf-8"))
        return len(rows)


class XLSXExporter(Exporter):
    format_name = "xlsx"
    mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def write(self, rows: Sequence[dict], out: BinaryIO) -> int:
        try:
            from openpyxl import Workbook
        except ImportError as exc:  # pragma: no cover
            raise NotFoundError("XLSX support requires the openpyxl package") from exc

        wb = Workbook(write_only=True)
        ws = wb.create_sheet("export")
        if rows:
            ws.append(list(rows[0].keys()))
        count = 0
        for row in rows:
            ws.append([row.get(k) for k in rows[0].keys()])
            count += 1
        wb.save(out)
        return count


EXPORTERS: dict[str, type[Exporter]] = {
    e.format_name: e for e in (CSVExporter, JSONExporter, XLSXExporter)
}


class ExportService:
    def __init__(self, files: FileService) -> None:
        self.files = files

    async def export(
        self,
        session: AsyncSession,
        *,
        format_name: str,
        rows: Iterable[dict],
        base_name: str,
        created_by=None,
        organization_id=None,
        metadata: dict | None = None,
    ):
        exporter_cls = EXPORTERS.get(format_name.lower())
        if exporter_cls is None:
            raise NotFoundError(f"Unsupported export format: {format_name}")
        exporter = exporter_cls()

        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{base_name}_{stamp}.{exporter.format_name}"

        # Render synchronously (CPU-bound, bounded input) then hand to storage.
        buffer = io.BytesIO()
        count = exporter.write(list(rows), buffer)
        buffer.seek(0)

        record = await self.files.store(
            session,
            content=buffer,
            filename=filename,
            mime_type=exporter.mime_type,
            category=FileCategory.EXPORT.value,
            created_by=created_by,
            organization_id=organization_id,
            metadata={**(metadata or {}), "row_count": count, "format": exporter.format_name},
        )
        logger.info(
            "Export generated",
            extra={"extra_fields": {"file_id": str(record.id), "format": exporter.format_name, "rows": count}},
        )
        return record
