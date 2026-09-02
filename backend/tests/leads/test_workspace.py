"""Phase 4 unit tests: normalization, quality scoring, models, workspace
services (create/update/status/search/filter/sort/pagination/tags/notes/
activity/bulk), saved views."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.errors import NotFoundError, ValidationError
from app.models.lead import LeadStatus, SavedView
from app.models.scrape import Lead
from app.services.leads import (
    LeadWorkspaceService,
    SavedViewService,
    TagService,
)
from app.services.leads.activity import LeadActivityService
from app.services.leads.normalization import normalize_lead_payload
from app.services.leads.quality import compute_quality_score


@pytest.mark.asyncio
async def test_normalization_email_phone_website():
    clean, errors = normalize_lead_payload(
        {
            "business_name": "  Acme   Industries  ",
            "email": "  Contact@ACME.com ",
            "phone": "+91 98765 43210",
            "website": "https://www.acme.example.com/path",
            "city": "Ahmedabad",
        }
    )
    assert errors == {}
    assert clean["business_name"] == "Acme Industries"
    assert clean["email"] == "Contact@ACME.com"
    assert clean["email_norm"] == "contact@acme.com"
    assert clean["phone"] == "+91 98765 43210"
    assert clean["phone_norm"] == "+919876543210"
    assert clean["website_norm"] == "acme.example.com"
    assert clean["name_key"].startswith("acme industries|ahmedabad")


@pytest.mark.asyncio
async def test_normalization_rejects_bad_values():
    clean, errors = normalize_lead_payload({"email": "not-an-email", "phone": "123", "website": "nope"})
    assert "email" in errors and "phone" in errors
    # 'nope' has no dot → not a usable host → rejected
    assert "website" in errors


@pytest.mark.asyncio
async def test_normalization_field_length_caps():
    clean, errors = normalize_lead_payload({"business_name": "x" * 500, "address": "y" * 900})
    assert errors == {}
    assert len(clean["business_name"]) == 300
    assert len(clean["address"]) == 500


def test_quality_score_deterministic():
    full = {"business_name": "A", "phone": "1", "email": "a@b.co", "website": "a.co",
            "address": "x", "city": "c", "state": "s", "source": "src"}
    assert compute_quality_score(full) == 100
    name_only = {"business_name": "A"}
    assert compute_quality_score(name_only) == 20
    assert compute_quality_score({"business_name": "A", "email": "a@b.co"}) == 40
    assert compute_quality_score({}) == 0
    # deterministic: same input, same score
    assert compute_quality_score(full) == compute_quality_score(dict(full))


@pytest.mark.asyncio
async def test_workspace_create_update_status_activity(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    lead = await svc.create_lead(
        session,
        {"business_name": "Zen Traders", "email": "owner@zentraders.in", "phone": "9876543210", "city": "Surat"},
        tags=["Hot", "Local Business"],
    )
    assert lead.status == "NEW"
    assert lead.email_norm == "owner@zentraders.in"
    assert lead.phone_norm == "9876543210"
    assert lead.quality_score == 70  # name+email+phone+city+provenance
    assert sorted(lead.tags) == ["Hot", "Local Business"]

    # relational tag truth
    from app.models.lead import LeadTag, LeadTagAssignment

    assignments = (await session.execute(
        select(LeadTag.name).join(
            LeadTagAssignment, LeadTagAssignment.tag_id == LeadTag.id
        ).where(LeadTagAssignment.lead_id == lead.id)
    )).scalars().all()
    assert sorted(assignments) == ["Hot", "Local Business"]

    # update normalizes and tracks changes
    updated = await svc.apply_update(
        session, lead, {"email": "NEW@zentraders.in", "website": "zentraders.in", "postal_code": "395003"}
    )
    assert updated.email == "NEW@zentraders.in"
    assert updated.email_norm == "new@zentraders.in"
    assert updated.website_norm == "zentraders.in"
    assert updated.quality_score == 85  # + website(15)

    # status workflow
    await svc.set_status(session, updated, "VERIFIED")
    assert updated.status == "VERIFIED"
    await svc.archive(session, updated)
    assert updated.status == "ARCHIVED" and updated.archived_at is not None
    await svc.restore(session, updated)
    assert updated.status == "NEW" and updated.archived_at is None

    # activity trail records everything
    activities, total = await LeadActivityService().list_for_lead(session, lead.id)
    kinds = {a.event_type for a in activities}
    assert {"lead_created", "lead_updated", "status_changed", "lead_archived", "lead_restored"} <= kinds
    assert total >= 5


@pytest.mark.asyncio
async def test_workspace_search_filter_sort_pagination(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    for i in range(60):
        await svc.create_lead(
            session,
            {
                "business_name": f"Shop {i:02d}",
                "email": f"shop{i}@test.in" if i % 2 == 0 else None,
                "phone": f"90000000{i:02d}",
                "city": "Ahmedabad" if i % 3 == 0 else "Surat",
                "state": "Gujarat",
            },
            source="test-source",
            commit=(i % 10 == 9),
        )
    await session.commit()

    # pagination envelope math
    rows, total = await svc.search(session, page=2, page_size=25)
    assert total == 60 and len(rows) == 25

    # search across columns
    rows, total = await svc.search(session, search="Shop 01")
    assert total == 1
    rows, total = await svc.search(session, search="ahmedabad")  # city column
    assert total == 20

    # advanced filter: state=Gujarat AND has_email — AND/OR groups
    spec = {
        "and": [
            {"field": "state", "op": "eq", "value": "Gujarat"},
            {"or": [
                {"field": "city", "op": "eq", "value": "Ahmedabad"},
                {"field": "city", "op": "eq", "value": "Surat"},
            ]},
        ]
    }
    rows, total = await svc.search(session, filters=spec)
    assert total == 60

    spec2 = {"and": [{"field": "state", "op": "eq", "value": "Gujarat"},
                     {"field": "has_email", "op": "eq", "value": True}]}
    rows, total = await svc.search(session, filters=spec2)
    assert total == 30

    # operators
    rows, total = await svc.search(session, filters={"field": "quality_score", "op": "gte", "value": 50})
    assert total == 60
    rows, total = await svc.search(session, filters={"field": "business_name", "op": "starts_with", "value": "Shop 1"})
    assert total == 10
    rows, total = await svc.search(session, filters={"field": "email", "op": "empty"})
    assert total == 30

    # sorting whitelist: deterministic order, newest first by default
    rows, _ = await svc.search(session, sort="business_name", page_size=5)
    assert rows[0].business_name == "Shop 00"
    rows, _ = await svc.search(session, sort="-business_name", page_size=5)
    assert rows[0].business_name == "Shop 59"

    # archived excluded by default, included on demand
    lead = rows[0]
    await svc.archive(session, lead)
    _, total_hidden = await svc.search(session)
    _, total_shown = await svc.search(session, include_archived=True)
    assert total_hidden == 59 and total_shown == 60


@pytest.mark.asyncio
async def test_filters_reject_injection_and_unknown_fields(seeded_db):
    from app.services.leads import filters as f

    with pytest.raises(ValidationError):
        f.build_filter_condition({"field": "password_hash", "op": "eq", "value": "x"})
    with pytest.raises(ValidationError):
        f.build_filter_condition({"field": "business_name", "op": "DROP TABLE leads", "value": "x"})
    with pytest.raises(ValidationError):
        f.build_order_by("business_name; DROP TABLE leads")
    with pytest.raises(ValidationError):
        f.build_filter_condition({"field": "quality_score", "op": "between", "value": "nope"})
    with pytest.raises(ValidationError):
        f.build_filter_condition({"xor": []})
    # deep nesting rejected
    deep = {"field": "city", "op": "eq", "value": "x"}
    for _ in range(5):
        deep = {"and": [deep]}
    with pytest.raises(ValidationError):
        f.build_filter_condition(deep)


@pytest.mark.asyncio
async def test_search_escapes_wildcards(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    await svc.create_lead(session, {"business_name": "100% Pure Water"})
    await svc.create_lead(session, {"business_name": "Pure Water Co"})
    # literal % must not act as a wildcard
    rows, total = await svc.search(session, search="100%")
    assert total == 1
    rows, total = await svc.search(session, search="_ure")
    assert total == 0


@pytest.mark.asyncio
async def test_tags_crud_and_bulk(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    tags = TagService()
    lead_a = await svc.create_lead(session, {"business_name": "A"})
    lead_b = await svc.create_lead(session, {"business_name": "B"})

    created = await tags.create(session, "Exporter", color="#ff0000")
    await tags.assign(session, lead_a.id, "Exporter")
    # idempotent
    tag, was_created = await tags.assign(session, lead_a.id, "exporter")
    assert was_created is False and tag.id == created.id

    await tags.bulk_assign(session, [lead_a.id, lead_b.id], ["Hot", "High Value"])
    assert sorted(lead_a.tags) == ["Exporter", "High Value", "Hot"]
    assert sorted(lead_b.tags) == ["High Value", "Hot"]

    # rename propagates to mirrors
    await tags.rename(session, created.id, "Export House")
    await session.refresh(lead_a)
    assert "Export House" in lead_a.tags and "Exporter" not in lead_a.tags

    counts = {t["name"]: t["lead_count"] for t in await tags.list(session)}
    assert counts["Hot"] == 2

    await tags.delete(session, created.id)
    await session.refresh(lead_a)
    assert "Export House" not in lead_a.tags

    # duplicate tag names rejected
    await tags.create(session, "UniqueTag")
    from app.core.errors import ConflictError

    with pytest.raises(ConflictError):
        await tags.create(session, "uniquetag")


@pytest.mark.asyncio
async def test_notes(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    lead = await svc.create_lead(session, {"business_name": "Note Co"})
    await svc.add_note(session, lead.id, "First contact")
    await svc.add_note(session, lead.id, "Wants a callback")
    notes, total = await svc.list_notes(session, lead.id)
    assert total == 2 and notes[0].content == "Wants a callback"
    with pytest.raises(ValidationError):
        await svc.add_note(session, lead.id, "   ")


@pytest.mark.asyncio
async def test_bulk_actions_efficient(seeded_db):
    session = seeded_db
    svc = LeadWorkspaceService()
    ids = []
    for i in range(30):
        lead = await svc.create_lead(session, {"business_name": f"Bulk {i}"}, commit=False)
        ids.append(lead.id)
    await session.commit()

    from sqlalchemy import event

    query_count = {"n": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):
        query_count["n"] += 1

    event.listen(session.sync_session.bind, "before_cursor_execute", _count)

    try:
        counts = await svc.bulk_action(session, action="add_tag", lead_ids=ids,
                                       params={"tags": ["Bulk Tag"]})
        assert counts["affected"] == 30  # 30 leads × 1 new tag
        counts = await svc.bulk_action(session, action="set_status", lead_ids=ids,
                                       params={"status": "QUALIFIED"})
        assert counts["affected"] == 30
    finally:
        event.remove(session.sync_session.bind, "before_cursor_execute", _count)

    rows, total = await svc.search(session, filters={"field": "tag", "op": "eq", "value": "Bulk Tag"})
    assert total == 30
    rows, total = await svc.search(session, filters={"field": "status", "op": "eq", "value": "QUALIFIED"})
    assert total == 30

    # soft delete (default)
    counts = await svc.bulk_action(session, action="delete", lead_ids=ids[:5])
    assert counts["affected"] == 5
    rows, total = await svc.search(session)
    assert total == 25

    # hard delete requires explicit confirm + cap
    with pytest.raises(ValidationError):
        await svc.bulk_action(session, action="delete", lead_ids=ids[:2],
                              params={"hard": True}, hard_delete_allowed=True)
    counts = await svc.bulk_action(
        session, action="delete", lead_ids=ids[:2],
        params={"hard": True, "confirm": "DELETE"}, hard_delete_allowed=True,
    )
    assert counts["affected"] == 2


@pytest.mark.asyncio
async def test_saved_views(seeded_db):
    session = seeded_db
    svc = SavedViewService()
    view = await svc.create(
        session, name="My Ahmedabad Leads",
        filters={"and": [{"field": "city", "op": "eq", "value": "Ahmedabad"},
                         {"field": "status", "op": "eq", "value": "NEW"},
                         {"field": "has_email", "op": "eq", "value": True}]},
        owner_id=uuid.uuid4(),
    )
    assert view.visibility == "PRIVATE"

    copy = await svc.duplicate(session, view.id, user_id=view.owner_id, can_manage_all=False)
    assert copy.name == "My Ahmedabad Leads (copy)"

    await svc.rename(session, view.id, name="Renamed", user_id=view.owner_id, can_manage_all=False)
    assert (await session.get(SavedView, view.id)).name == "Renamed"

    # other users cannot touch a private view they don't own
    from app.core.errors import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        await svc.rename(session, view.id, name="Hijacked", user_id=uuid.uuid4(), can_manage_all=False)

    # invalid filter spec rejected at save time
    with pytest.raises(ValidationError):
        await svc.create(session, name="Broken", filters={"field": "nope", "op": "eq", "value": 1})

    await svc.delete(session, copy.id, user_id=copy.owner_id, can_manage_all=False)
    with pytest.raises(NotFoundError):
        await svc.get_visible(session, copy.id, user_id=copy.owner_id)


@pytest.mark.asyncio
async def test_status_validation_allows_codes_rejects_junk(seeded_db):
    """Default statuses work; custom codes must look like canonical codes;
    junk/lowercase values are rejected (§3 configurable, not lawless)."""
    session = seeded_db
    svc = LeadWorkspaceService()
    lead = await svc.create_lead(session, {"business_name": "Status Co"})

    await svc.set_status(session, lead, "QUALIFIED")
    assert lead.status == "QUALIFIED"

    # custom uppercase code is allowed (configurable status vocabulary)
    await svc.set_status(session, lead, "DO_NOT_CALL")
    assert lead.status == "DO_NOT_CALL"

    for junk in ("new", "Has Space", "NEWW!", "x", "", "a" * 40):
        try:
            await svc.set_status(session, lead, junk)
            raise AssertionError(f"status {junk!r} should have been rejected")
        except Exception as exc:  # ValidationError
            assert "Unknown status" in str(getattr(exc, "message", str(exc)))
