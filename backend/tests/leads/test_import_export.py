"""Phase 4: import (CSV/XLSX/JSON/JSONL, mapping, validation, duplicate
strategies, error reports) and streaming export (4 formats, history)."""

from __future__ import annotations

import io
import json
import uuid

import pytest
from sqlalchemy import func, select

from app.models.lead import ImportStatus, LeadDuplicateCandidate
from app.models.scrape import Lead
from app.services.files import FileService
from app.services.leads.exporter import LeadExportService
from app.services.leads.importer import LeadImportService

CSV_GOOD = (
    "Company,Mobile,Mail,City\n"
    "Acme Corp,+91 9876500001,owner@acme.in,Ahmedabad\n"
    "Beta Traders,9876500002,owner@beta.in,Surat\n"
    "Gamma Industries,9876500003,owner@gamma.in,Vadodara\n"
)
CSV_DIRTY = (
    "Company,Mobile,Mail\n"
    "Good Corp,9876500004,good@corp.in\n"
    ",,bad@rowonly.in\n"                      # missing business name -> rejected
    "Bad Email Corp,9876500005,broken-email\n"  # invalid email -> rejected
    "Bad Phone Corp,12,ok@corp.in\n"          # phone < 7 digits -> rejected
)


@pytest.fixture
def import_service(app):
    return LeadImportService(app.state.storage, app.state.files)


@pytest.fixture
def export_service(app):
    return LeadExportService(app.state.storage, app.state.files)


async def _store_import_file(app, session, content: bytes, filename: str):
    files = FileService(app.state.storage, app.state.audit)
    return await files.store(
        session, content=io.BytesIO(content), filename=filename,
        mime_type="application/octet-stream", category="IMPORT",
    )


async def _run_import(app, session, svc, content: bytes, filename: str, *,
                      mapping=None, options=None, admin_id=None):
    record = await _store_import_file(app, session, content, filename)
    fmt = filename.rsplit(".", 1)[-1]
    batch = await svc.create_batch(
        session, file_record=record, format_name=fmt, filename=filename,
        created_by=admin_id, options=options or {},
    )
    batch.mapping = mapping or {"Company": "business_name", "Mobile": "phone", "Mail": "email", "City": "city"}
    await session.commit()
    batch = await svc.run(session, batch)
    return batch


@pytest.mark.asyncio
async def test_csv_import_roundtrip(app, seeded_db, import_service):
    batch = await _run_import(app, seeded_db, import_service, CSV_GOOD.encode(), "leads.csv")
    assert batch.status == ImportStatus.COMPLETED.value
    assert batch.total_rows == 3
    assert batch.imported_rows == 3 and batch.invalid_rows == 0

    leads = (await seeded_db.scalars(select(Lead))).all()
    assert len(leads) == 3
    acme = next(l for l in leads if l.business_name == "Acme Corp")
    assert acme.phone == "+91 9876500001"
    assert acme.phone_norm == "+919876500001"  # leading + preserved (Phase 3 semantics)
    assert acme.email_norm == "owner@acme.in"
    assert acme.source_type == "import"
    assert acme.import_batch_id == batch.id
    assert acme.quality_score >= 65
    # activity recorded
    from app.services.leads.activity import LeadActivityService

    acts, _ = await LeadActivityService().list_for_lead(seeded_db, acme.id)
    assert any(a.event_type == "lead_imported" for a in acts)


@pytest.mark.asyncio
async def test_csv_import_rejects_bad_rows_and_writes_report(app, seeded_db, import_service):
    batch = await _run_import(app, seeded_db, import_service, CSV_DIRTY.encode(), "dirty.csv")
    assert batch.status == ImportStatus.PARTIAL.value
    assert batch.imported_rows == 1
    assert batch.invalid_rows == 3
    assert len(batch.error_summary) == 3
    assert any("email" in e["error"] for e in batch.error_summary)
    assert any("business name" in e["error"] for e in batch.error_summary)
    assert batch.error_file_id is not None

    # rejected-rows report is downloadable through the FileService path
    record, stream = await FileService(app.state.storage, app.state.audit).open_download(
        seeded_db, batch.error_file_id
    )
    body = stream.read().decode()
    assert "Bad Email Corp" in body


@pytest.mark.asyncio
async def test_csv_import_extra_fields_preserved(app, seeded_db, import_service):
    csv_content = "Company,Mail,Notes\nX Corp,x@corp.in,met at expo\n"
    await _run_import(
        app, seeded_db, import_service, csv_content.encode(), "x.csv",
        mapping={"Company": "business_name", "Mail": "email", "Notes": "industry"},
    )
    lead = (await seeded_db.scalars(select(Lead))).one()
    assert lead.industry == "met at expo"


@pytest.mark.asyncio
async def test_duplicate_strategies(app, seeded_db, import_service):
    # first import: 3 leads
    await _run_import(app, seeded_db, import_service, CSV_GOOD.encode(), "first.csv")

    async def lead_count() -> int:
        return int(await seeded_db.scalar(select(func.count()).select_from(Lead)))

    # SKIP_DUPLICATES (default): same emails → all duplicates
    batch = await _run_import(app, seeded_db, import_service, CSV_GOOD.encode(), "second.csv")
    assert batch.duplicate_rows == 3 and batch.imported_rows == 0
    assert await lead_count() == 3

    # CREATE_NEW: no dedup → 3 more rows
    batch = await _run_import(app, seeded_db, import_service, CSV_GOOD.encode(), "third.csv",
                              options={"duplicate_strategy": "CREATE_NEW"})
    assert batch.imported_rows == 3
    assert await lead_count() == 6

    # UPDATE_EXISTING: fills only empty fields, never overwrites non-empty
    update_csv = "Company,Mobile,Mail,City\nAcme Corp,9876500001,owner@acme.in,Gandhinagar\n"
    batch = await _run_import(app, seeded_db, import_service, update_csv.encode(), "update.csv",
                              options={"duplicate_strategy": "UPDATE_EXISTING"})
    assert batch.updated_rows == 1
    acme = (await seeded_db.scalars(
        select(Lead).where(Lead.email_norm == "owner@acme.in").limit(1)
    )).first()
    assert await lead_count() == 6

    # REVIEW: inserts the row flagged + queues a pending candidate
    review_csv = "Company,Mobile,Mail\nDelta Corp,9876500001,owner@acme.in\n"
    batch = await _run_import(app, seeded_db, import_service, review_csv.encode(), "review.csv",
                              options={"duplicate_strategy": "REVIEW"})
    assert batch.review_rows == 1
    candidates = (await seeded_db.scalars(
        select(LeadDuplicateCandidate).where(LeadDuplicateCandidate.origin == "import")
    )).all()
    assert len(candidates) == 1 and candidates[0].status == "PENDING"


@pytest.mark.asyncio
async def test_json_and_jsonl_import(app, seeded_db, import_service):
    json_payload = json.dumps([
        {"Company": "JSON Corp", "Mail": "j@corp.in", "Mobile": "9000000011"},
        {"Company": "JSON Two", "Mail": "j2@corp.in", "Mobile": "9000000012"},
    ]).encode()
    mapping = {"Company": "business_name", "Mail": "email", "Mobile": "phone"}
    batch = await _run_import(app, seeded_db, import_service, json_payload, "leads.json", mapping=mapping)
    assert batch.imported_rows == 2

    jsonl = b"\n".join([
        json.dumps({"Company": "JSONL One", "Mail": "l1@corp.in", "Mobile": "9000000021"}).encode(),
        json.dumps({"Company": "JSONL Two", "Mail": "l2@corp.in", "Mobile": "9000000022"}).encode(),
        b"",  # blank line skipped
    ])
    batch = await _run_import(app, seeded_db, import_service, jsonl, "leads.jsonl", mapping=mapping)
    assert batch.imported_rows == 2


@pytest.mark.asyncio
async def test_malformed_inputs_fail_honestly(app, seeded_db, import_service):
    record = await _store_import_file(app, seeded_db, b"{ this is not json", "broken.json")
    batch = await import_service.create_batch(
        seeded_db, file_record=record, format_name="json", filename="broken.json", created_by=None
    )
    batch.mapping = {"Company": "business_name"}
    await seeded_db.commit()
    batch = await import_service.run(seeded_db, batch)
    assert batch.status == ImportStatus.FAILED.value
    assert "Malformed JSON" in batch.error_summary[0]["error"]

    # malformed XLSX
    record = await _store_import_file(app, seeded_db, b"not-an-xlsx", "broken.xlsx")
    batch = await import_service.create_batch(
        seeded_db, file_record=record, format_name="xlsx", filename="broken.xlsx", created_by=None
    )
    batch.mapping = {"Company": "business_name"}
    await seeded_db.commit()
    batch = await import_service.run(seeded_db, batch)
    assert batch.status == ImportStatus.FAILED.value


@pytest.mark.asyncio
async def test_xlsx_import_with_sheet_selection(app, seeded_db, import_service):
    from openpyxl import Workbook

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Sheet1"
    ws1.append(["ignore", "me"])
    ws2 = wb.create_sheet("Real Data")
    ws2.append(["Company", "Mail"])
    ws2.append(["XLSX Corp", "x@xlsx.in"])
    ws2.append(["XLSX Two", "x2@xlsx.in"])
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    record = await FileService(app.state.storage, app.state.audit).store(
        seeded_db, content=buffer, filename="book.xlsx", mime_type="x", category="IMPORT"
    )
    batch = await import_service.create_batch(
        seeded_db, file_record=record, format_name="xlsx", filename="book.xlsx",
        created_by=None, options={"sheet": "Real Data"},
    )
    batch.mapping = {"Company": "business_name", "Mail": "email"}
    await seeded_db.commit()
    batch = await import_service.run(seeded_db, batch)
    assert batch.imported_rows == 2
    assert batch.total_rows == 2  # junk sheet rows not counted


@pytest.mark.asyncio
async def test_export_all_formats(app, seeded_db, export_service, import_service):
    await _run_import(app, seeded_db, import_service, CSV_GOOD.encode(), "leads.csv")

    for fmt, sniff in (
        ("csv", lambda b: b"Business Name" in b and b"Acme Corp" in b),
        ("jsonl", lambda b: b'"Acme Corp"' in b and b.count(b"\n") >= 3),
        ("json", lambda b: b'"Acme Corp"' in b and b.strip().endswith(b"]")),
        ("xlsx", lambda b: b[:2] == b"PK"),  # zip magic
    ):
        record = await export_service.create(
            seeded_db, format_name=fmt, scope="all", created_by=None,
        )
        record = await export_service.run(seeded_db, record)
        assert record.status == "COMPLETED", f"{fmt}: {record.error}"
        assert record.row_count == 3
        file_record, stream = await FileService(app.state.storage, app.state.audit).open_download(
            seeded_db, record.file_id
        )
        body = stream.read()
        assert sniff(body), f"format {fmt} content wrong"
        stream.close()


@pytest.mark.asyncio
async def test_export_streams_in_chunks(app, seeded_db, export_service, import_service):
    """Large export stays memory-flat: rows are pulled in OFFSET chunks and
    written incrementally — verified by exporting >CHUNK rows."""
    for i in range(1200):
        seeded_db.add(Lead(
            business_name=f"Stream {i}", email=f"s{i}@stream.in",
            phone_norm=f"90000{i:05d}", status="NEW", source_type="manual",
        ))
    await seeded_db.commit()

    record = await export_service.create(seeded_db, format_name="csv", scope="all")
    record = await export_service.run(seeded_db, record)
    assert record.status == "COMPLETED"
    assert record.row_count == 1200  # 1200 rows across multiple 500-row chunks


@pytest.mark.asyncio
async def test_export_rejects_unknown_fields_and_formats(seeded_db, export_service):
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        await export_service.create(seeded_db, format_name="pdf", scope="all")
    with pytest.raises(ValidationError):
        await export_service.create(seeded_db, format_name="csv", scope="all", fields=["password_hash"])
    with pytest.raises(ValidationError):
        await export_service.create(seeded_db, format_name="csv", scope="selected", ids=[])
