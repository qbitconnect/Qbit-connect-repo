"""Phase 4: DuplicateDetectionService + safe merge semantics."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.lead import (
    DuplicateConfidence,
    DuplicateStatus,
    LeadMergeHistory,
)
from app.services.leads import DuplicateDetectionService, LeadWorkspaceService, MergeService


@pytest.mark.asyncio
async def _make_lead(session, svc, **fields):
    return await svc.create_lead(session, fields)


@pytest.mark.asyncio
async def test_confidence_ladder(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    det = DuplicateDetectionService()

    base = await _make_lead(
        session, svc, business_name="Alpha Industries", email="owner@alpha.in",
        phone="9876500001", website="alpha.in", city="Rajkot",
    )

    # EXACT: same normalized email
    twin_email = await _make_lead(session, svc, business_name="Completely Different Name", email="OWNER@ALPHA.IN")
    # EXACT: same normalized phone
    twin_phone = await _make_lead(session, svc, business_name="Third Name Here", phone="9876500001")
    # HIGH: same website host
    twin_web = await _make_lead(session, svc, business_name="Fourth Business", website="https://www.alpha.in/about")
    # MEDIUM: business name + city (name_key)
    twin_name = await _make_lead(session, svc, business_name="Alpha Industries", city="Rajkot", phone="9111100000")
    # LOW: fuzzy name only
    twin_fuzzy = await _make_lead(session, svc, business_name="Alpha Industries Pvt Ltd", city="Bhavnagar", phone="9222200000")

    matches = {m.lead.id: m for m in await det.find_duplicates(session, base)}

    assert matches[twin_email.id].confidence is DuplicateConfidence.EXACT
    assert matches[twin_phone.id].confidence is DuplicateConfidence.EXACT
    assert matches[twin_web.id].confidence is DuplicateConfidence.HIGH
    assert matches[twin_name.id].confidence is DuplicateConfidence.MEDIUM
    assert matches[twin_fuzzy.id].confidence is DuplicateConfidence.LOW


@pytest.mark.asyncio
async def test_scan_creates_pending_candidates_once(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    det = DuplicateDetectionService()
    a = await _make_lead(session, svc, business_name="Scan Industries", email="scan@industries.in", phone="9000000001")
    b = await _make_lead(session, svc, business_name="Scan Industries", email="scan2@industries.in", phone="9000000002")

    created = await det.scan(session)
    assert created >= 1
    # scan is idempotent at the pair level
    created_again = await det.scan(session)
    assert created_again == 0

    candidates, total = await det.list_candidates(session, status="PENDING")
    pair = {candidates[0][0].lead_a_id, candidates[0][0].lead_b_id}
    assert pair == {a.id, b.id}


@pytest.mark.asyncio
async def test_merge_fills_empty_keeps_primary_records_conflicts(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    merge = MergeService()

    primary = await _make_lead(
        session, svc, business_name="Primary Corp", phone="9876511111",
        city="Vadodara", email=None,
    )
    merged = await _make_lead(
        session, svc, business_name="Primary Corporation", phone="9876511111",
        email="info@primarycorp.in", city="Vadodara", website="primarycorp.in",
    )
    # tag on the merged side must survive the merge
    await MergeService().tags.assign(session, merged.id, "Survivor")
    from app.services.leads import TagService

    await TagService().assign(session, merged.id, "Survivor")

    result = await merge.merge(session, primary_id=primary.id, merged_id=merged.id)

    # primary's values kept where present; empties filled from merged
    assert result.phone == "9876511111"
    assert result.email == "info@primarycorp.in"
    assert result.website == "www.primarycorp.in" or result.website == "primarycorp.in"
    assert result.business_name == "Primary Corp"  # primary value wins on conflict
    assert result.city == "Vadodara"
    assert result.quality_score >= 80

    # merged lead is soft-retired, never deleted
    await session.refresh(merged)
    assert merged.status == "ARCHIVED"
    assert merged.merged_into_id == primary.id

    # evidence
    history = (await session.scalars(select(LeadMergeHistory))).all()
    assert len(history) == 1
    row = history[0]
    assert row.primary_lead_id == primary.id
    assert row.merged_lead_id == merged.id
    assert "business_name" in (row.conflicts or {})  # conflict preserved, not lost
    assert row.before_data["merged"]["business_name"] == "Primary Corporation"

    # tags survived
    tags_now = set(result.tags or [])
    assert "Survivor" in tags_now

    # activity trail
    from app.services.leads.activity import LeadActivityService

    activities, _ = await LeadActivityService().list_for_lead(session, primary.id)
    assert any(a.event_type == "duplicate_merged" for a in activities)


@pytest.mark.asyncio
async def test_merge_rejections(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    merge = MergeService()
    a = await _make_lead(session, svc, business_name="Self", email="self@x.in")
    b = await _make_lead(session, svc, business_name="Other", email="other@x.in")

    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        await merge.merge(session, primary_id=a.id, merged_id=a.id)

    await merge.merge(session, primary_id=a.id, merged_id=b.id)
    # cannot merge an already-merged lead again
    c = await _make_lead(session, svc, business_name="Third", email="third@x.in")
    with pytest.raises(ValidationError):
        await merge.merge(session, primary_id=c.id, merged_id=b.id)
