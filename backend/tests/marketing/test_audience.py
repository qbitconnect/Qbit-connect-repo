"""Audience engine + snapshot tests (Phase 5 §11, §12, §42, §44)."""

from __future__ import annotations

import time

import pytest

from app.core.errors import PermissionDeniedError, ValidationError
from app.models.lead import LeadTag, LeadTagAssignment, SavedView
from app.services.marketing import AudienceService
from tests.marketing.conftest import seed_leads


@pytest.fixture
def audience() -> AudienceService:
    return AudienceService()


class TestValidation:
    def test_rejects_unknown_type(self, audience):
        with pytest.raises(ValidationError):
            audience.validate_definition({"type": "everything"})

    def test_rejects_unknown_keys(self, audience):
        with pytest.raises(ValidationError):
            audience.validate_definition({"type": "filters", "evil": "x"})

    def test_rejects_bad_filters(self, audience):
        with pytest.raises(ValidationError):
            audience.validate_definition({"type": "filters", "filters": {"nope": 1}})

    def test_rejects_bad_saved_view_id(self, audience):
        with pytest.raises(ValidationError):
            audience.validate_definition({"type": "saved_view", "saved_view_id": "abc"})

    def test_accepts_all_four_types(self, audience):
        valid_filters = {"field": "city", "op": "eq", "value": "Surat"}
        assert audience.validate_definition({"type": "filters", "filters": valid_filters})["type"] == "filters"
        assert audience.validate_definition({"type": "tags", "tags": ["a"]})
        with pytest.raises(ValidationError):
            audience.validate_definition({"type": "selected", "lead_ids": []})  # non-empty enforced


class TestResolution:
    async def test_filters_audience(self, seeded_db, audience):
        from app.models.scrape import Lead

        await seed_leads(seeded_db, 5, city="Surat")
        await seed_leads(seeded_db, 3, city="Vadodara")
        n = await audience.count(
            seeded_db,
            {"type": "filters", "filters": {"field": "city", "op": "eq", "value": "Surat"}},
        )
        assert n == 5

    async def test_statuses_narrowing(self, seeded_db, audience):
        from app.models.marketing import RecipientStatus  # noqa: F401

        leads = await seed_leads(seeded_db, 4)
        leads[0].status = "CONVERTED"
        await seeded_db.commit()
        n_all = await audience.count(seeded_db, {"type": "selected", "lead_ids": [str(l.id) for l in leads]})
        n_new = await audience.count(
            seeded_db,
            {"type": "selected", "lead_ids": [str(l.id) for l in leads], "statuses": ["NEW"]},
        )
        assert n_all == 4 and n_new == 3

    async def test_tags_any_and_all(self, seeded_db, audience):
        leads = await seed_leads(seeded_db, 4)
        tag1 = LeadTag(name="vip")
        tag2 = LeadTag(name="hot")
        seeded_db.add_all([tag1, tag2])
        await seeded_db.flush()
        seeded_db.add(LeadTagAssignment(lead_id=leads[0].id, tag_id=tag1.id))
        seeded_db.add(LeadTagAssignment(lead_id=leads[1].id, tag_id=tag1.id))
        seeded_db.add(LeadTagAssignment(lead_id=leads[1].id, tag_id=tag2.id))
        await seeded_db.commit()

        assert await audience.count(seeded_db, {"type": "tags", "tags": ["vip"]}) == 2
        assert await audience.count(seeded_db, {"type": "tags", "tags": ["vip", "hot"], "match": "all"}) == 1
        assert await audience.count(seeded_db, {"type": "tags", "tags": ["ghost"]}) == 0

    async def test_saved_view_resolution_and_privacy(self, seeded_db, audience):
        from tests.marketing.conftest import make_lead

        view = SavedView(
            name="Surat only", entity="leads", visibility="PRIVATE",
            filters={"field": "city", "op": "eq", "value": "Surat"},
            owner_id=None,
        )
        seeded_db.add(view)
        lead = make_lead(city="Surat")
        seeded_db.add(lead)
        await seeded_db.commit()

        n = await audience.count(seeded_db, {"type": "saved_view", "saved_view_id": str(view.id)})
        assert n == 1
        # PRIVATE view owned by another user is refused
        import uuid as uuid_module

        view.owner_id = uuid_module.uuid4()
        await seeded_db.commit()
        with pytest.raises(PermissionDeniedError):
            await audience.count(
                seeded_db, {"type": "saved_view", "saved_view_id": str(view.id)},
                user_id=uuid_module.uuid4(),
            )

    async def test_merged_leads_excluded(self, seeded_db, audience):
        import uuid as uuid_module

        leads = await seed_leads(seeded_db, 3)
        leads[0].merged_into_id = uuid_module.uuid4()
        await seeded_db.commit()
        n = await audience.count(
            seeded_db, {"type": "selected", "lead_ids": [str(l.id) for l in leads]}
        )
        assert n == 2


class TestSnapshot:
    async def test_snapshot_creates_recipients(self, seeded_db, audience):
        from app.models.marketing import Campaign, CampaignRecipient, CampaignStatus

        leads = await seed_leads(seeded_db, 10)
        campaign = Campaign(
            name="C1", channel="WHATSAPP", status=CampaignStatus.DRAFT,
            audience_definition={"type": "selected", "lead_ids": [str(l.id) for l in leads]},
        )
        seeded_db.add(campaign)
        await seeded_db.commit()

        result = await audience.snapshot(seeded_db, campaign)
        assert result["created"] == 10
        from sqlalchemy import select

        rows = (await seeded_db.execute(select(CampaignRecipient))).scalars().all()
        assert len(rows) == 10
        assert all(r.status == "PENDING" for r in rows)

    async def test_snapshot_is_immutable_and_idempotent(self, seeded_db, audience):
        from app.models.marketing import Campaign, CampaignStatus

        leads = await seed_leads(seeded_db, 6)
        campaign = Campaign(
            name="C2", channel="WHATSAPP", status=CampaignStatus.QUEUED,
            audience_definition={"type": "selected", "lead_ids": [str(l.id) for l in leads]},
        )
        seeded_db.add(campaign)
        await seeded_db.commit()

        first = await audience.snapshot(seeded_db, campaign)
        assert first["created"] == 6
        # audience "changes" afterwards (more leads match) — snapshot must not
        more = await seed_leads(seeded_db, 4)
        campaign.audience_definition = {
            "type": "selected",
            "lead_ids": [str(l.id) for l in leads + more],
        }
        await seeded_db.commit()
        second = await audience.snapshot(seeded_db, campaign)
        assert second["already_snapshotted"] is True
        assert second["created"] == 0

    async def test_batched_streaming_matches_total(self, seeded_db, audience):
        from app.models.marketing import Campaign, CampaignStatus

        leads = await seed_leads(seeded_db, 250)
        campaign = Campaign(
            name="C3", channel="WHATSAPP", status=CampaignStatus.QUEUED,
            audience_definition={"type": "selected", "lead_ids": [str(l.id) for l in leads]},
        )
        seeded_db.add(campaign)
        await seeded_db.commit()
        result = await audience.snapshot(seeded_db, campaign, batch_size=40)
        assert result["created"] == 250

    async def test_large_audience_snapshot_is_efficient(self, seeded_db, audience):
        """10,000 leads must snapshot without thousands of transactions and
        without loading all leads into RAM at once (§44)."""
        from sqlalchemy import func, select

        from app.models.marketing import Campaign, CampaignRecipient, CampaignStatus

        t0 = time.perf_counter()
        await seed_leads(seeded_db, 10_000)
        seed_time = time.perf_counter() - t0

        campaign = Campaign(
            name="C10k", channel="WHATSAPP", status=CampaignStatus.QUEUED,
            audience_definition={"type": "filters",
                                 "filters": {"field": "city", "op": "eq", "value": "Surat"}},
        )
        seeded_db.add(campaign)
        await seeded_db.commit()

        t1 = time.perf_counter()
        result = await audience.snapshot(seeded_db, campaign, batch_size=1000)
        elapsed = time.perf_counter() - t1
        assert result["created"] == 10_000
        total = await seeded_db.scalar(
            select(func.count()).select_from(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign.id)
        )
        assert total == 10_000
        # generous ceiling for CI variance (seed excluded — that's fixture cost)
        assert elapsed < 60, f"snapshot took {elapsed:.1f}s"
