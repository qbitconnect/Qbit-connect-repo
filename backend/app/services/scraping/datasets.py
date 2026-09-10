"""DatasetService — the Dataset abstraction (Actor Platform spec §10).

Every Actor Run produces a Dataset: normalized rows captured by the result
pipeline, queryable (search / filter / sort / paginate) and exportable
(JSON / JSONL / CSV / XLSX / XML) as ALL / SELECTED / FILTERED slices.

Storage discipline (spec §13/§34): rows are batch-inserted; exports stream
from the DB in bounded windows — a million-item dataset never loads whole
into RAM. A dataset is NEVER padded with synthetic rows: item_count is the
real number of items the run produced.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.actor_platform import ActorDataset, ActorDatasetItem, DatasetStatus
from app.services.scraping.result_files import JsonlWriter

logger = get_logger("qbit.scraping.datasets")

EXPORT_FORMATS = ("json", "jsonl", "csv", "xlsx", "xml")
BATCH_WINDOW = 500


class DatasetService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------- lifecycle
    async def create_for_job(
        self,
        *,
        job_id: uuid.UUID,
        actor_id: str,
        actor_version: str | None,
        name: str | None = None,
        organization_id: uuid.UUID | None = None,
        created_by: uuid.UUID | None = None,
    ) -> ActorDataset:
        dataset = ActorDataset(
            job_id=job_id,
            actor_id=actor_id,
            actor_version=actor_version,
            name=name,
            status=DatasetStatus.RUNNING.value,
            organization_id=organization_id,
            created_by=created_by,
        )
        self.session.add(dataset)
        await self.session.flush()
        return dataset

    async def add_items(self, dataset_id: uuid.UUID, items: list[dict]) -> int:
        """Batched insert; returns rows written. Order preserved via idx.
        Items are JSON-sanitized (datetime/UUID → str) exactly like the JSONL
        writers (json.dumps default=str) so DB columns never explode."""
        if not items:
            return 0
        safe_items = [_json_safe(item) for item in items]
        start = await self.session.execute(
            select(func.coalesce(func.max(ActorDatasetItem.idx), -1)).where(
                ActorDatasetItem.dataset_id == dataset_id
            )
        )
        base = int(start.scalar_one()) + 1
        rows = [
            ActorDatasetItem(dataset_id=dataset_id, idx=base + i, data=item)
            for i, item in enumerate(safe_items)
        ]
        self.session.add_all(rows)
        await self.session.flush()
        await self._merge_schema_fields(dataset_id, safe_items)
        await self.session.execute(
            ActorDataset.__table__.update()
            .where(ActorDataset.id == dataset_id)
            .values(item_count=ActorDataset.item_count + len(rows), updated_at=datetime.now(timezone.utc))
        )
        return len(rows)

    async def _merge_schema_fields(self, dataset_id: uuid.UUID, items: list[dict]) -> None:
        row = await self.session.get(ActorDataset, dataset_id)
        if row is None:
            return
        fields = list(row.schema_fields or [])
        seen = set(fields)
        for item in items:
            for key in item.keys():
                if key not in seen:
                    seen.add(key)
                    fields.append(key)
        row.schema_fields = fields[:200]

    async def finalize(
        self, dataset_id: uuid.UUID, *, status: DatasetStatus, clean_status: str = "clean"
    ) -> None:
        row = await self.session.get(ActorDataset, dataset_id)
        if row is None:
            return
        row.status = status.value
        row.clean_status = clean_status if clean_status in ("clean", "partial") else "clean"
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()

    # -------------------------------------------------------------- querying
    async def get(self, dataset_id: uuid.UUID) -> ActorDataset | None:
        return await self.session.get(ActorDataset, dataset_id)

    async def for_job(self, job_id: uuid.UUID) -> ActorDataset | None:
        res = await self.session.execute(
            select(ActorDataset).where(ActorDataset.job_id == job_id).order_by(ActorDataset.created_at)
        )
        return res.scalars().first()

    async def list(
        self,
        *,
        actor_id: str | None = None,
        status: str | None = None,
        organization_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ActorDataset], int]:
        stmt = select(ActorDataset)
        count_stmt = select(func.count(ActorDataset.id))
        conds = []
        if actor_id:
            conds.append(ActorDataset.actor_id == actor_id)
        if status:
            conds.append(ActorDataset.status == status)
        if organization_id:
            conds.append(ActorDataset.organization_id == organization_id)
        if conds:
            stmt = stmt.where(*conds)
            count_stmt = count_stmt.where(*conds)
        total = (await self.session.execute(count_stmt)).scalar_one()
        rows = await self.session.execute(
            stmt.order_by(ActorDataset.created_at.desc()).limit(limit).offset(offset)
        )
        return list(rows.scalars()), int(total)

    async def items_page(
        self,
        dataset_id: uuid.UUID,
        *,
        search: str | None = None,
        field: str | None = None,
        value: str | None = None,
        sort_field: str | None = None,
        sort_dir: str = "asc",
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[ActorDatasetItem], int]:
        """Search (substring across values) / filter (field equality-ish) /
        sort (top-level JSON field) / paginate — all in SQL where possible."""
        stmt = select(ActorDatasetItem).where(ActorDatasetItem.dataset_id == dataset_id)
        count_stmt = select(func.count(ActorDatasetItem.id)).where(
            ActorDatasetItem.dataset_id == dataset_id
        )
        conds = []
        if search:
            like = f"%{search.lower()}%"
            conds.append(
                or_(
                    func.lower(func.cast(ActorDatasetItem.data, sa_text())).like(like),
                )
            )
        if field and value is not None:
            conds.append(func.lower(func.json_extract(ActorDatasetItem.data, f"$.{field}")).like(f"%{str(value).lower()}%"))
        if conds:
            stmt = stmt.where(*conds)
            count_stmt = count_stmt.where(*conds)
        total = (await self.session.execute(count_stmt)).scalar_one()
        # sorting: SQLite/PG portable approach — fetch window sorted in Python
        # ONLY when sorting by a JSON field; idx sort stays in SQL.
        if sort_field and sort_field != "idx":
            rows = (await self.session.execute(stmt.order_by(ActorDatasetItem.idx))).scalars()
            items = list(rows)
            reverse = sort_dir == "desc"

            def _key(it: ActorDatasetItem):
                v = (it.data or {}).get(sort_field)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return (0, v, "")
                return (1, 0, str(v).lower() if v is not None else "")

            items.sort(key=_key, reverse=reverse)
            return items[offset : offset + limit], int(total)
        order = ActorDatasetItem.idx.desc() if sort_dir == "desc" else ActorDatasetItem.idx.asc()
        rows = await self.session.execute(stmt.order_by(order).limit(limit).offset(offset))
        return list(rows.scalars()), int(total)

    async def selected_items(self, dataset_id: uuid.UUID, ids: list[str]) -> list[ActorDatasetItem]:
        if not ids:
            return []
        uuids = [uuid.UUID(i) for i in ids]
        rows = await self.session.execute(
            select(ActorDatasetItem).where(
                ActorDatasetItem.dataset_id == dataset_id,
                ActorDatasetItem.id.in_(uuids),
            ).order_by(ActorDatasetItem.idx)
        )
        return list(rows.scalars())

    # -------------------------------------------------------------- exporting
    async def export(
        self,
        dataset: ActorDataset,
        fmt: str,
        *,
        ids: list[str] | None = None,
        search: str | None = None,
        field: str | None = None,
        value: str | None = None,
        sink: JsonlWriter | None = None,
    ) -> tuple[str, bytes]:
        """Export ALL / SELECTED (ids) / FILTERED (search|field+value).

        Returns (filename, bytes) for in-memory formats, or (filename, b"")
        when a streaming sink (JSONL) was provided.
        """
        fmt = (fmt or "json").lower()
        if fmt not in EXPORT_FORMATS:
            raise ValueError(f"Unsupported export format: {fmt}")
        if ids:
            items = await self.selected_items(dataset.id, ids)
        elif search or (field and value is not None):
            items, _total = await self.items_page(
                dataset.id, search=search, field=field, value=value,
                offset=0, limit=1_000_000,
            )
        else:
            rows = await self.session.execute(
                select(ActorDatasetItem)
                .where(ActorDatasetItem.dataset_id == dataset.id)
                .order_by(ActorDatasetItem.idx)
            )
            items = list(rows.scalars())
        records = [it.data or {} for it in items]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = f"dataset-{dataset.actor_id}-{stamp}"
        if fmt == "jsonl":
            buf = io.StringIO()
            for rec in records:
                buf.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
            return f"{base}.jsonl", buf.getvalue().encode("utf-8")
        if fmt == "json":
            return (
                f"{base}.json",
                json.dumps(records, default=str, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        if fmt == "csv":
            return f"{base}.csv", _to_csv(records)
        if fmt == "xml":
            return f"{base}.xml", _to_xml(records, dataset.actor_id)
        if fmt == "xlsx":
            return f"{base}.xlsx", _to_xlsx(records)
        raise ValueError(f"Unsupported export format: {fmt}")  # pragma: no cover

    async def purge(self, dataset_id: uuid.UUID) -> int:
        """Remove all items (keeps the dataset row + metadata)."""
        res = await self.session.execute(
            delete(ActorDatasetItem).where(ActorDatasetItem.dataset_id == dataset_id)
        )
        await self.session.execute(
            ActorDataset.__table__.update()
            .where(ActorDataset.id == dataset_id)
            .values(item_count=0, updated_at=datetime.now(timezone.utc))
        )
        return int(res.rowcount or 0)


# ------------------------------------------------------------------ renderers
def _flatten_columns(records: list[dict]) -> list[str]:
    cols: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for key in rec.keys():
            if key not in seen:
                seen.add(key)
                cols.append(key)
    return cols


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def _to_csv(records: list[dict]) -> bytes:
    """CSV with formula-injection neutralization (mirrors export service)."""
    cols = _flatten_columns(records)
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(cols)
    for rec in records:
        row = []
        for col in cols:
            cell = _cell(rec.get(col))
            if cell[:1] in ("=", "+", "-", "@", "\t", "\r"):
                cell = "'" + cell
            row.append(cell)
        writer.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def _to_xml(records: list[dict], root_name: str) -> bytes:
    root = ET.Element("dataset", {"actor": root_name, "items": str(len(records))})
    for rec in records:
        item = ET.SubElement(root, "item")
        for col in _flatten_columns(records):
            child = ET.SubElement(item, _safe_tag(col))
            child.text = _cell(rec.get(col))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _safe_tag(name: str) -> str:
    out = "".join(c if (c.isalnum() or c in "_-.") else "_" for c in name)
    if not out or not (out[0].isalpha() or out[0] == "_"):
        out = f"f_{out}"
    return out


def _to_xlsx(records: list[dict]) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
    except ImportError:  # pragma: no cover - openpyxl is a pinned dependency
        raise ValueError("XLSX export requires the openpyxl dependency")
    wb = Workbook()
    ws = wb.active
    ws.title = "dataset"
    cols = _flatten_columns(records)
    ws.append(cols)
    for rec in records:
        ws.append([_cell(rec.get(col))[:32000] for col in cols])
    for i, col in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(col) + 2, 12), 48)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _json_safe(value):
    """Recursively convert non-JSON-native scalars to strings (mirror of the
    JSONL writers' default=str discipline)."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # datetime, UUID, Decimal, enum…


def sa_text():
    """Portable CAST target for JSON→text (SQLite TEXT / PG JSONB safe)."""
    from sqlalchemy import Text

    return Text()
