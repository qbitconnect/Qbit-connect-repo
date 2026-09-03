"""Suppression + unsubscribe token tests (Phase 7 §13, §14, §25, §47, §51)."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.services.marketing.suppression import SuppressionService, UnsubscribeService
from tests.marketing.helpers import TEST_SECRET, make_suppression


@pytest_asyncio.fixture
async def session(app):
    db = app.state.db
    async with db.session() as s:
        yield s


@pytest_asyncio.fixture
def unsub(app):
    return UnsubscribeService(ttl_days=30, base_url="https://app.test")


class TestSuppression:
    async def test_add_is_idempotent(self, session: AsyncSession):
        svc = SuppressionService()
        row1 = await svc.add(session, channel="EMAIL", address="A@Example.com", reason="MANUAL")
        row2 = await svc.add(session, channel="EMAIL", address="a@example.com", reason="MANUAL")
        assert row1.id == row2.id

    async def test_is_suppressed_matches_normalized(self, session: AsyncSession):
        await make_suppression(session, email="blocked@example.com")
        found = await SuppressionService().is_suppressed(
            session, channel="EMAIL", address_norm="blocked@example.com"
        )
        assert found is not None

    async def test_terminal_reason_cannot_be_removed(self, session: AsyncSession):
        await make_suppression(session, email="gone@example.com", reason="UNSUBSCRIBED")
        with pytest.raises(ValidationError):
            await SuppressionService().remove(
                session, channel="EMAIL", address_norm="gone@example.com", actor=None
            )

    async def test_manual_can_be_removed(self, session: AsyncSession):
        await make_suppression(session, email="temp@example.com", reason="MANUAL")
        removed = await SuppressionService().remove(
            session, channel="EMAIL", address_norm="temp@example.com", actor=None
        )
        assert removed


class TestUnsubscribeTokens:
    async def test_issue_and_confirm(self, session: AsyncSession, unsub):
        raw = await unsub.issue_token(
            session,
            channel="EMAIL",
            address="user@example.com",
            address_norm="user@example.com",
            lead_id=None,
            campaign_id=None,
            recipient_id=None,
        )
        # raw token is unguessable and NOT any embedded id
        assert "user@example.com" not in raw
        assert len(raw) >= 32

        token = await unsub.resolve(session, raw)
        assert token.used_at is None

        await unsub.confirm(session, raw, ip="1.2.3.4")
        assert token.used_at is not None

        suppression = await SuppressionService().is_suppressed(
            session, channel="EMAIL", address_norm="user@example.com"
        )
        assert suppression is not None and suppression.reason == "UNSUBSCRIBED"

    async def test_token_single_use(self, session: AsyncSession, unsub):
        raw = await unsub.issue_token(
            session, channel="EMAIL", address="u2@example.com", address_norm="u2@example.com",
            lead_id=None, campaign_id=None, recipient_id=None,
        )
        await unsub.confirm(session, raw)
        with pytest.raises(ValidationError):
            await unsub.confirm(session, raw)

    async def test_token_hashed_at_rest(self, session: AsyncSession, unsub):
        from sqlalchemy import select

        from app.models.marketing import UnsubscribeToken

        raw = await unsub.issue_token(
            session, channel="EMAIL", address="u3@example.com", address_norm="u3@example.com",
            lead_id=None, campaign_id=None, recipient_id=None,
        )
        rows = (await session.scalars(select(UnsubscribeToken))).all()
        assert len(rows) == 1
        stored = rows[0].token_hash
        assert raw not in stored and stored != raw
        assert len(stored) == 64  # sha256 hex

    async def test_unknown_token_rejected(self, session: AsyncSession, unsub):
        with pytest.raises(NotFoundError):
            await unsub.resolve(session, "totally-made-up-token-" + "x" * 40)

    async def test_unguessable_randomness(self, session: AsyncSession, unsub):
        raws = {
            await unsub.issue_token(
                session, channel="EMAIL", address=f"u{i}@example.com",
                address_norm=f"u{i}@example.com", lead_id=None, campaign_id=None, recipient_id=None,
            )
            for i in range(5)
        }
        assert len(raws) == 5  # no collisions, no predictable sequence

    async def test_url_building(self, unsub):
        assert unsub.build_url("tok").startswith("https://app.test/unsubscribe/")
