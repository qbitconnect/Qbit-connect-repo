"""Eligibility engine + suppression + opt-out tests (Phase 5 §13, §14, §15)."""

from __future__ import annotations

import pytest

from app.services.marketing.eligibility import EligibilityService
from app.services.marketing.suppression import SuppressionService
from tests.marketing.conftest import make_lead


@pytest.fixture
def eligibility() -> EligibilityService:
    return EligibilityService()


@pytest.fixture
def suppression() -> SuppressionService:
    return SuppressionService()


class TestEligibilityDecisions:
    def test_eligible_lead(self, eligibility):
        lead = make_lead()
        status, reason = eligibility.check_recipient(
            channel="WHATSAPP", lead=lead, suppressed=False, suppress_reason=None,
        )
        assert status == "ELIGIBLE" and reason is None

    def test_missing_phone(self, eligibility):
        lead = make_lead(phone=None)
        status, reason = eligibility.check_recipient(
            channel="WHATSAPP", lead=lead, suppressed=False, suppress_reason=None,
        )
        assert status == "INELIGIBLE" and reason == "MISSING_PHONE"

    def test_missing_email(self, eligibility):
        lead = make_lead(email=None)
        status, reason = eligibility.check_recipient(
            channel="EMAIL", lead=lead, suppressed=False, suppress_reason=None,
        )
        assert status == "INELIGIBLE" and reason == "MISSING_EMAIL"

    def test_invalid_address(self, eligibility):
        lead = make_lead(phone="12")
        status, reason = eligibility.check_recipient(
            channel="WHATSAPP", lead=lead, suppressed=False, suppress_reason=None,
        )
        assert status == "INELIGIBLE" and reason == "INVALID_ADDRESS"

    def test_suppressed(self, eligibility):
        lead = make_lead()
        status, reason = eligibility.check_recipient(
            channel="WHATSAPP", lead=lead, suppressed=True, suppress_reason="SUPPRESSED",
        )
        assert status == "INELIGIBLE" and reason == "SUPPRESSED"

    def test_unsubscribed_reason_wins(self, eligibility):
        lead = make_lead()
        status, reason = eligibility.check_recipient(
            channel="WHATSAPP", lead=lead, suppressed=True, suppress_reason="UNSUBSCRIBED",
        )
        assert status == "INELIGIBLE" and reason == "UNSUBSCRIBED"

    def test_no_opt_in_scraped_data_is_not_consent(self, eligibility):
        """THE consent rule: a scraped email/phone is NOT automatic consent."""
        lead = make_lead()
        lead.metadata_json = {}  # no marketing_opt_in flag
        status, reason = eligibility.check_recipient(
            channel="WHATSAPP", lead=lead, suppressed=False, suppress_reason=None,
        )
        assert status == "INELIGIBLE" and reason == "NO_OPT_IN"

    def test_archived_lead(self, eligibility):
        lead = make_lead(status="ARCHIVED")
        status, reason = eligibility.check_recipient(
            channel="WHATSAPP", lead=lead, suppressed=False, suppress_reason=None,
        )
        assert status == "INELIGIBLE" and reason == "LEAD_UNAVAILABLE"

    def test_merged_lead(self, eligibility):
        import uuid

        lead = make_lead(merged_into_id=uuid.uuid4())
        status, reason = eligibility.check_recipient(
            channel="WHATSAPP", lead=lead, suppressed=False, suppress_reason=None,
        )
        assert status == "INELIGIBLE" and reason == "LEAD_UNAVAILABLE"

    def test_unknown_channel(self, eligibility):
        status, reason = eligibility.check_recipient(
            channel="PIGEON", lead=make_lead(), suppressed=False, suppress_reason=None,
        )
        assert status == "INELIGIBLE" and reason == "CHANNEL_UNAVAILABLE"


class TestSuppression:
    async def test_add_and_check(self, seeded_db, suppression):
        await suppression.add(
            seeded_db, entry_type="PHONE", address="+91 98765 43210",
            reason="MANUAL", channel="WHATSAPP",
        )
        suppressed = await suppression.is_suppressed(
            seeded_db, channel="WHATSAPP", phone="+919876543210",
        )
        assert suppressed[0] is True

    async def test_channel_wide_entry_applies_to_all_channels(self, seeded_db, suppression):
        await suppression.add(
            seeded_db, entry_type="EMAIL", address="bad@acme.test", reason="BLOCKED",
            channel=None,
        )
        for channel in ("WHATSAPP", "EMAIL", "SMS"):
            hit, _ = await suppression.is_suppressed(
                seeded_db, channel=channel, email="bad@acme.test",
            )
            assert hit is True

    async def test_idempotent_add(self, seeded_db, suppression):
        first = await suppression.add(
            seeded_db, entry_type="EMAIL", address="dup@acme.test", reason="MANUAL",
        )
        second = await suppression.add(
            seeded_db, entry_type="EMAIL", address="dup@acme.test", reason="MANUAL",
        )
        assert first.id == second.id

    async def test_opt_out_creates_suppression_and_cannot_be_removed(self, seeded_db, suppression):
        from app.core.errors import ValidationError

        record = await suppression.record_opt_out(
            seeded_db, channel="EMAIL", address="bye@acme.test", source="reply",
        )
        assert record.reason == "UNSUBSCRIBED"
        entries, total = await suppression.list_entries(seeded_db, entry_type="EMAIL")
        assert total == 1
        hit, reason = await suppression.is_suppressed(
            seeded_db, channel="EMAIL", email="bye@acme.test",
        )
        assert hit and reason == "UNSUBSCRIBED"
        with pytest.raises(ValidationError):
            await suppression.remove(seeded_db, entries[0].id)

    async def test_normal_suppression_can_be_removed(self, seeded_db, suppression):
        entry = await suppression.add(
            seeded_db, entry_type="EMAIL", address="temp@acme.test", reason="MANUAL",
        )
        await suppression.remove(seeded_db, entry.id)
        _entries, total = await suppression.list_entries(seeded_db)
        assert total == 0

    async def test_lead_level_suppression(self, seeded_db, suppression):
        lead = make_lead()
        seeded_db.add(lead)
        await seeded_db.commit()
        await suppression.add(
            seeded_db, entry_type="LEAD", address=str(lead.id), reason="COMPLAINT",
        )
        hit, _ = await suppression.is_suppressed(
            seeded_db, channel="WHATSAPP", lead_id=lead.id,
        )
        assert hit is True

    async def test_batch_check_efficient_keys(self, seeded_db, suppression):
        from tests.marketing.conftest import seed_leads

        leads = await seed_leads(seeded_db, 20)
        await suppression.add(
            seeded_db, entry_type="PHONE", address="+919876543210", reason="MANUAL",
            channel="WHATSAPP",
        )
        result = await suppression.check_batch(
            seeded_db, channel="WHATSAPP",
            emails=[l.email for l in leads],
            phones=[l.phone for l in leads],
            lead_ids=[l.id for l in leads],
        )
        # phone +919876543210 belongs to lead0 → suppressed
        assert result["suppressed"]["phone:+919876543210"][0] is True
        # everyone else clean
        assert result["suppressed"]["phone:+919876543211"][0] is False
