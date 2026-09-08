"""Lead export system (Phase 4 §27, §28, §38): CSV / XLSX / JSON / JSONL.

Memory-flat by design: rows are pulled in chunks (yield-per-batch) and written
through incremental writers to a spooled temp file on disk; the finished file
is registered via FileService (storage category EXPORT) — clients only ever
receive file ids. Large exports are queued and processed by the background
worker; small ones run inline. Every export is recorded in lead_exports.
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import uuid
from datetime import datetime, timezone
from typing import BinaryIO, Iterator

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.lead import ExportStatus, LeadExportRecord
from app.models.scrape import Lead
from app.services.files import FileService
from app.services.leads import filters as filter_engine
from app.services.storage import StorageService

logger = get_logger("qbit.leads.export")

CHUNK = 500

#: whitelisted export columns (§38: all fields / visible fields / custom)
EXPORT_FIELDS: dict[str, str] = {
    "id": "Id",
    "business_name": "Business Name",
    "contact_name": "Contact Name",
    "first_name": "First Name",
    "last_name": "Last Name",
    "email": "Email",
    "phone": "Phone",
    "website": "Website",
    "address": "Address",
    "city": "City",
    "state": "State",
    "postal_code": "Postal Code",
    "country": "Country",
    "category": "Category",
    "industry": "Industry",
    "status": "Status",
    "quality_score": "Quality Score",
    "source": "Source",
    "source_type": "Source Type",
    "source_id": "Source Id",
    "source_url": "Source URL",
    "source_actor_id": "Scraper",
    "source_actor_version": "Scraper Version",
    "source_job_id": "Scrape Job",
    "import_batch_id": "Import Batch",
    "scraped_at": "Scraped At",
    "created_at": "Created",
    "updated_at": "Updated",
    "last_verified_at": "Last Verified",
    "tags": "Tags",
    "rating": "Rating",
    "review_count": "Review Count",
    "social_links": "Social Links",
    "metadata": "Metadata",
}

FORMATS = ("csv", "xlsx", "json", "jsonl")
SCOPES = ("selected", "filtered", "page", "all", "lead")


def lead_to_row(lead: Lead, fields: list[str]) -> dict:
    """Flatten a lead into export cells (JSON-ish columns stringified)."""
    data = lead.to_public_dict()
    row = {}
    for field in fields:
        value = data.get(field)
        if field == "tags":
            value = "; ".join(data.get("tags") or [])
        elif field in ("metadata", "social_links"):
            value = json.dumps(value, default=str, ensure_ascii=False) if value else ""
        row[EXPORT_FIELDS.get(field, field)] = "" if value is None else value
    return row


def _formula_safe_cell(value):
    """Phase 12 (audit L1): neutralize CSV/Excel formula injection.

    Lead fields are free-text and may originate from SCRAPED content. A cell
    beginning with = + - @ would be interpreted as a formula by Excel/Libre
    Office/Google Sheets when the operator opens the export (DDE/SQL/command
    payloads are a real exfiltration vector). String cells with those lead
    characters get a leading apostrophe; numeric types pass through intact.
    """
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value


class _CSVWriter:
    format_name = "csv"
    mime_type = "text/csv"

    def __init__(self, handle: BinaryIO, fields: list[str]) -> None:
        self._text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
        self._writer = csv.writer(self._text)
        self._writer.writerow([EXPORT_FIELDS[f] for f in fields])
        self.count = 0

    def write_row(self, row: dict) -> None:
        self._writer.writerow([_formula_safe_cell(v) for v in row.values()])
        self.count += 1

    def close(self) -> None:
        self._text.flush()
        self._text.detach()


class _JSONLWriter:
    format_name = "jsonl"
    mime_type = "application/x-ndjson"

    def __init__(self, handle: BinaryIO, fields: list[str]) -> None:
        self._handle = handle
        self.count = 0

    def write_row(self, row: dict) -> None:
        self._handle.write(
            json.dumps(row, default=str, ensure_ascii=False).encode("utf-8") + b"\n"
        )
        self.count += 1

    def close(self) -> None:
        self._handle.flush()


class _JSONWriter:
    format_name = "json"
    mime_type = "application/json"

    def __init__(self, handle: BinaryIO, fields: list[str]) -> None:
        self._handle = handle
        self._first = True
        handle.write(b"[")
        self.count = 0

    def write_row(self, row: dict) -> None:
        prefix = b"" if self._first else b",\n"
        self._first = False
        self._handle.write(prefix + json.dumps(row, default=str, ensure_ascii=False).encode("utf-8"))
        self.count += 1

    def close(self) -> None:
        self._handle.write(b"]")
        self._handle.flush()


class _XLSXWriter:
    format_name = "xlsx"
    mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def __init__(self, handle: BinaryIO, fields: list[str]) -> None:
        from openpyxl import Workbook

        self._handle = handle
        self._wb = Workbook(write_only=True)
        self._ws = self._wb.create_sheet("leads")
        self._ws.append([EXPORT_FIELDS[f] for f in fields])
        self.count = 0

    def write_row(self, row: dict) -> None:
        self._ws.append([_formula_safe_cell(v) for v in row.values()])
        self.count += 1

    def close(self) -> None:
        self._wb.save(self._handle)


_WRITERS = {"csv": _CSVWriter, "jsonl": _JSONLWriter, "json": _JSONWriter, "xlsx": _XLSXWriter}


class LeadExportService:
    def __init__(self, storage: StorageService, files: FileService) -> None:
        self.storage = storage
        self.files = files

    # ------------------------------------------------------------- lifecycle
    async def create(
        self,
        session: AsyncSession,
        *,
        format_name: str,
        scope: str = "filtered",
        filters: dict | list | None = None,
        search: str | None = None,
        sort: str | None = None,
        ids: list[uuid.UUID] | None = None,
        lead_id: uuid.UUID | None = None,
        fields: list[str] | None = None,
        page: int = 1,
        page_size: int = 100,
        created_by: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
    ) -> LeadExportRecord:
        format_name = (format_name or "").lower()
        if format_name not in FORMATS:
            raise ValidationError(f"Unsupported export format: {format_name}")
        scope = (scope or "filtered").lower()
        if scope not in SCOPES:
            raise ValidationError(f"Unknown export scope: {scope}")
        if scope == "selected" and not ids:
            raise ValidationError("Selected-scope export requires lead ids")
        if scope == "lead" and lead_id is None:
            raise ValidationError("Lead-scope export requires a lead id")
        fields = self._validate_fields(fields)

        if filters:
            filter_engine.build_filter_condition(filters)  # validate eagerly

        record = LeadExportRecord(
            format=format_name,
            scope=scope,
            filters={
                "filters": filters,
                "search": search,
                "sort": sort,
                "ids": [str(i) for i in (ids or [])][:10000],
                "lead_id": str(lead_id) if lead_id else None,
                "page": page,
                "page_size": page_size,
            },
            fields=fields,
            status=ExportStatus.QUEUED.value,
            created_by=created_by,
            organization_id=organization_id,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record

    @staticmethod
    def _validate_fields(fields: list[str] | None) -> list[str]:
        if not fields:
            return list(EXPORT_FIELDS.keys())
        clean = []
        for field in fields:
            field = str(field).strip()
            if field not in EXPORT_FIELDS:
                raise ValidationError(f"Unknown export field: {field}")
            if field not in clean:
                clean.append(field)
        return clean or list(EXPORT_FIELDS.keys())

    async def count(self, session: AsyncSession, record: LeadExportRecord) -> int:
        query = self._base_query(record)
        return int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)

    def _base_query(self, record: LeadExportRecord):
        params = record.filters or {}
        query = select(Lead).where(Lead.merged_into_id.is_(None))
        scope = record.scope
        if scope == "lead":
            lead_id = uuid.UUID(params["lead_id"])
            query = query.where(Lead.id == lead_id)
        elif scope == "selected":
            ids = [uuid.UUID(x) for x in (params.get("ids") or [])]
            if not ids:
                raise ValidationError("Selected-scope export has no ids")
            query = query.where(Lead.id.in_(ids))
        elif scope == "page":
            page = int(params.get("page") or 1)
            page_size = int(params.get("page_size") or 100)
            sub = query.order_by(Lead.created_at, Lead.id).offset((page - 1) * page_size).limit(page_size).subquery()
            return select(Lead).where(
                Lead.id.in_(select(sub.c.id))
            )
        elif scope in ("filtered", "all"):
            if scope == "filtered":
                if params.get("search"):
                    # reuse the workspace search builder
                    from app.services.leads.service import LeadWorkspaceService

                    condition = LeadWorkspaceService().search_condition(params["search"])
                    if condition is not None:
                        query = query.where(condition)
                if params.get("filters"):
                    query = query.where(filter_engine.build_filter_condition(params["filters"]))
            else:
                query = query.where(Lead.status != "ARCHIVED")
        return query

    async def run(self, session: AsyncSession, record: LeadExportRecord, *, worker_owner: str | None = None) -> LeadExportRecord:
        """Stream the export to storage and finalize the history row."""
        if record.status not in (ExportStatus.QUEUED.value, ExportStatus.PROCESSING.value):
            raise ValidationError(f"Export is not runnable (status={record.status})")
        record.status = ExportStatus.PROCESSING.value
        record.started_at = record.started_at or datetime.now(timezone.utc)
        if worker_owner:
            record.lease_owner = worker_owner
            record.leased_at = datetime.now(timezone.utc)
        await session.commit()

        fields = self._validate_fields(record.fields)
        writer_cls = _WRITERS[record.format]
        handle = tempfile.NamedTemporaryFile(
            suffix=f".{record.format}", prefix=f"lead-export-{str(record.id)[:8]}-", delete=False
        )
        try:
            with handle:
                writer = writer_cls(handle, fields)
                query = self._base_query(record).order_by(Lead.created_at, Lead.id)
                offset = 0
                while True:
                    leads = (
                        await session.scalars(query.offset(offset).limit(CHUNK))
                    ).all()
                    if not leads:
                        break
                    for lead in leads:
                        writer.write_row(lead_to_row(lead, fields))
                    record.row_count = writer.count  # live progress
                    await session.commit()
                    offset += CHUNK
                writer.close()

            record.row_count = writer.count
            with open(handle.name, "rb") as finished:
                file_record = await self.files.store(
                    session,
                    content=finished,
                    filename=f"leads-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.{record.format}",
                    mime_type=writer_cls.mime_type,
                    category="EXPORT",
                    created_by=record.created_by,
                    organization_id=record.organization_id,
                    metadata={"lead_export_id": str(record.id), "scope": record.scope, "row_count": writer.count},
                )
            record.file_id = file_record.id
            record.status = ExportStatus.COMPLETED.value
            record.completed_at = datetime.now(timezone.utc)
            record.error = None
        except Exception as exc:
            record.status = ExportStatus.FAILED.value
            record.completed_at = datetime.now(timezone.utc)
            record.error = str(exc)[:2000]
            await session.commit()
            logger.exception("Export failed", extra={"extra_fields": {"export_id": str(record.id)}})
            raise
        finally:
            import os

            try:
                os.unlink(handle.name)
            except OSError:  # pragma: no cover
                pass
        await session.commit()
        logger.info(
            "Export completed",
            extra={"extra_fields": {"export_id": str(record.id), "rows": record.row_count, "format": record.format}},
        )
        return record

    async def claim_next(self, session: AsyncSession, owner: str) -> LeadExportRecord | None:
        row = (
            await session.scalars(
                select(LeadExportRecord)
                .where(LeadExportRecord.status == ExportStatus.QUEUED.value)
                .order_by(LeadExportRecord.created_at)
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        result = await session.execute(
            update(LeadExportRecord)
            .where(
                LeadExportRecord.id == row.id,
                LeadExportRecord.status == ExportStatus.QUEUED.value,
            )
            .values(status=ExportStatus.PROCESSING.value, lease_owner=owner,
                    leased_at=datetime.now(timezone.utc))
        )
        await session.commit()
        return row if (result.rowcount or 0) else None
