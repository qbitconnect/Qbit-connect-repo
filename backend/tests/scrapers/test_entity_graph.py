"""Source-graph evidence tests (spec §QBIT DIFFERENTIATION #1/#2).

Two items with the SAME business+location but different contact data run
through the real pipeline: MEDIUM-confidence match inserts BOTH leads and
records an ACTIVE EntityLink with matched_by evidence + actor provenance.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.scrape import EntityLink, Lead
from tests.scrapers.test_runner_pipeline import _make_job, _get_job


class _TwoSourcesActor:
    """Same business captured twice with different contact data."""

    id = "fake"
    version = "1.0.0"

    async def initialize(self, ctx):
        pass

    async def run(self, ctx):
        yield {
            "business_name": "Spice Garden",
            "city": "New Delhi",
            "email": "maps@spicegarden.test",
            "phone": "+911100000011",
            "source": "google-maps",
        }
        yield {
            "business_name": "Spice Garden",
            "city": "New Delhi",
            "email": "web@spicegarden.test",
            "website": "https://spicegarden.test",
            "source": "website",
        }

    async def cleanup(self, ctx):
        pass


@pytest.mark.asyncio
async def test_medium_match_records_entity_link(scrape_env):
    settings, db, queue, runner, _root = scrape_env
    job_id = await _make_job(db, input={"seed": 1})
    status = await runner.execute(job_id, _TwoSourcesActor())
    assert status == "COMPLETED"

    async with db.session() as session:
        leads = (await session.scalars(select(Lead).order_by(Lead.created_at))).all()
        assert len(leads) == 2  # MEDIUM match stays information-preserving
        links = (await session.scalars(select(EntityLink))).all()
        assert len(links) == 1
        link = links[0]
        pair = {str(link.lead_id), str(link.related_lead_id)}
        assert pair == {str(leads[0].id), str(leads[1].id)}
        assert link.relation == "SAME_BUSINESS"
        assert link.status == "ACTIVE"
        assert link.matched_by in ("name_key", "name+location", "phone", "email", "website")
        assert link.detected_by_job_id == job_id
        # platform-provenance: both ends carry the ACTOR THAT RAN (the pipeline
        # normalizes item source to the executing actor id "fake")
        assert link.source_actor_a == "fake"
        assert link.source_actor_b == "fake"
