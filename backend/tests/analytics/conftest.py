"""Phase 10 analytics test fixtures.

Builds realistic operational data directly at the model layer (fast and
deterministic): leads with provenance, scrape jobs, campaigns with immutable
events, conversations with messages, workflow executions. The analytics layer
then computes real aggregates over them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
import pytest_asyncio

UTC = dt_timezone.utc


async def _login(client, email, password):
    from httpx import AsyncClient

    resp = await client.post("/api/v1/auth/login",
                             json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest_asyncio.fixture
async def admin_auth(client):
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    return await _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest_asyncio.fixture
async def viewer_auth(client):
    from tests.conftest import VIEWER_EMAIL, VIEWER_PASSWORD

    return await _login(client, VIEWER_EMAIL, VIEWER_PASSWORD)


def days_ago(n: int, hour: int = 12) -> datetime:
    """Deterministic timestamp n days before now (fixed hour)."""
    base = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return base - timedelta(days=n)


async def make_lead(session, *, status="NEW", source="google_maps", created_at=None,
                    email=None, phone=None, quality=70, city="Pune", country="India",
                    category="cafe", industry="food", actor="google-maps",
                    version="1.0.0", merged_into=None, user_id=None,
                    source_type="scraper"):
    from app.models.scrape import Lead

    lead = Lead(
        business_name=f"Business {quality}-{status}",
        status=status, source=source, source_type=source_type,
        source_actor_id=actor, source_actor_version=version,
        email_norm=email, email=email, phone_norm=phone, phone=phone,
        quality_score=quality, city=city, country=country,
        category=category, industry=industry,
        merged_into_id=merged_into, created_by=user_id,
        created_at=created_at or days_ago(1),
        updated_at=created_at or days_ago(1),
    )
    session.add(lead)
    await session.flush()
    return lead


async def make_scrape_job(session, *, actor="google-maps", version="1.0.0",
                          status="COMPLETED", found=10, saved=8, dupes=1,
                          rejected=1, created_at=None, started_at=None,
                          completed_at=None, error=None, error_code=None):
    from app.models.scrape import ScrapeJob

    job = ScrapeJob(
        actor_id=actor, actor_version=version, status=status,
        records_found=found, records_saved=saved, records_duplicate=dupes,
        records_failed=rejected, error=error, error_code=error_code,
        created_at=created_at or days_ago(1),
        started_at=started_at or (created_at or days_ago(1)),
        completed_at=completed_at or (created_at or days_ago(1)) + timedelta(minutes=5),
        updated_at=created_at or days_ago(1),
    )
    session.add(job)
    await session.flush()
    return job


async def make_campaign(session, *, channel="WHATSAPP", status="COMPLETED",
                        account=None, name="Campaign", created_at=None,
                        started_at=None, completed_at=None):
    from app.models.marketing import Campaign

    campaign = Campaign(
        name=name, channel=channel, status=status,
        sending_account_id=account,
        created_at=created_at or days_ago(2),
        started_at=started_at or days_ago(2),
        completed_at=completed_at,
        updated_at=created_at or days_ago(2),
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def make_recipient(session, campaign_id, *, status="DELIVERED",
                         lead=None, address="user@example.com",
                         created_at=None, sent_at=None, delivered_at=None,
                         read_at=None, replied_at=None, failed_at=None,
                         bounced_at=None, complained_at=None, opened_at=None,
                         clicked_at=None):
    from app.models.marketing import CampaignRecipient

    row = CampaignRecipient(
        campaign_id=campaign_id, lead_id=lead.id if lead else None,
        recipient_address=address, status=status,
        sent_at=sent_at, delivered_at=delivered_at, read_at=read_at,
        replied_at=replied_at, failed_at=failed_at, bounced_at=bounced_at,
        complained_at=complained_at, opened_at=opened_at, clicked_at=clicked_at,
        created_at=created_at or days_ago(2),
        updated_at=created_at or days_ago(2),
    )
    session.add(row)
    await session.flush()
    return row


async def make_event(session, campaign_id, event_type, *, recipient_id=None,
                     created_at=None, provider="whatsapp_cloud",
                     provider_event_id=None, metadata=None):
    from app.models.marketing import CampaignEvent

    row = CampaignEvent(
        campaign_id=campaign_id, recipient_id=recipient_id,
        event_type=event_type, provider=provider,
        provider_event_id=provider_event_id,
        payload_metadata=metadata or {},
        created_at=created_at or days_ago(2),
    )
    session.add(row)
    await session.flush()
    return row


async def make_conversation_with_messages(
    session, *, channel="WHATSAPP", status="OPEN", created_at=None,
    closed_at=None, assigned_user=None, account=None,
    inbound_at_offsets=((0, 5), (2, 30)), contact_phone="+911234567890",
    lead=None,
):
    """Creates a conversation plus inbound/outbound message pairs.
    inbound_at_offsets: list of (day_offset, minute_offset) tuples for inbound
    messages; each is answered by an outbound message 2 minutes later."""
    from app.models.messaging import Conversation, Message

    created_at = created_at or days_ago(3)
    convo = Conversation(
        channel=channel, status=status, contact_phone=contact_phone,
        assigned_user_id=assigned_user, sending_account_id=account,
        created_at=created_at, updated_at=created_at, closed_at=closed_at,
        lead_id=lead.id if lead else None,
    )
    session.add(convo)
    await session.flush()
    for day_offset, minute_offset in inbound_at_offsets:
        inbound_at = created_at + timedelta(days=day_offset, minutes=minute_offset)
        session.add(Message(
            conversation_id=convo.id, direction="IN", status="RECEIVED",
            created_at=inbound_at, body="hello",
        ))
        session.add(Message(
            conversation_id=convo.id, direction="OUT", status="SENT",
            created_at=inbound_at + timedelta(minutes=2),
            sent_at=inbound_at + timedelta(minutes=2),
            delivered_at=inbound_at + timedelta(minutes=3),
            body="hi!",
        ))
    await session.flush()
    return convo


async def make_workflow_execution(session, workflow, version, *, status="COMPLETED",
                                  created_at=None, started_at=None,
                                  completed_at=None, error=None, error_class=None,
                                  steps=None):
    from app.models.automation import WorkflowExecution, WorkflowExecutionStep

    created_at = created_at or days_ago(1)
    execution = WorkflowExecution(
        workflow_id=workflow.id, workflow_version_id=version.id,
        trigger_event_id=f"evt-{workflow.id}-{int(created_at.timestamp())}-{status}",
        entity_type="lead", status=status, error=error, error_class=error_class,
        created_at=created_at, started_at=started_at or created_at,
        completed_at=completed_at or (created_at + timedelta(seconds=4)
                                      if status in ("COMPLETED", "FAILED") else None),
        updated_at=created_at,
    )
    session.add(execution)
    await session.flush()
    for node_type, step_status in (steps or [("TRIGGER", "COMPLETED"), ("ACTION", "COMPLETED")]):
        session.add(WorkflowExecutionStep(
            execution_id=execution.id, node_id=f"node-{node_type.lower()}",
            node_type=node_type, status=step_status,
            started_at=created_at, completed_at=created_at + timedelta(seconds=1),
        ))
    await session.flush()
    return execution


@pytest_asyncio.fixture
async def analytics_data(app):
    """A realistic cross-domain dataset (all real rows, deterministic)."""
    from app.core.security import hash_password
    from app.models.automation import Workflow, WorkflowVersion
    from app.models.marketing import EventType
    from app.models.user import User

    db = app.state.db
    async with db.session() as session:
        operator = User(email="operator-team@qbit.example.com",
                        password_hash=hash_password("Op3ratorSecret!Pass"),
                        full_name="Team Operator")
        session.add(operator)
        await session.flush()

        # --- leads: three sources, several statuses, one merged duplicate ----
        fresh = await make_lead(session, status="NEW", source="google_maps",
                                created_at=days_ago(1), quality=80)
        verified = await make_lead(session, status="VERIFIED", source="website",
                                   email="v@example.com", created_at=days_ago(2),
                                   quality=90)
        qualified = await make_lead(session, status="QUALIFIED", source="google_maps",
                                    created_at=days_ago(3), quality=75)
        contacted = await make_lead(session, status="CONTACTED", source="website",
                                    email="c@example.com", created_at=days_ago(4))
        interested = await make_lead(session, status="INTERESTED", source="google_maps",
                                     created_at=days_ago(5))
        converted = await make_lead(session, status="CONVERTED", source="google_maps",
                                    created_at=days_ago(6))
        lost = await make_lead(session, status="LOST", source="manual_import",
                               source_type="import", created_at=days_ago(6))
        dup = await make_lead(session, status="NEW", source="google_maps",
                              created_at=days_ago(2), merged_into=converted.id)
        await session.flush()

        # --- scrape jobs -------------------------------------------------------
        await make_scrape_job(session, actor="google-maps", status="COMPLETED",
                              found=20, saved=15, dupes=3, rejected=2,
                              created_at=days_ago(2))
        await make_scrape_job(session, actor="google-maps", version="1.1.0",
                              status="FAILED", found=5, saved=0, dupes=0, rejected=5,
                              created_at=days_ago(3), error_code="PROVIDER_ERROR",
                              error="provider boom")
        await make_scrape_job(session, actor="website", status="RUNNING",
                              found=0, saved=0, dupes=0, rejected=0,
                              created_at=days_ago(0, hour=1))
        await session.flush()

        # --- whatsapp campaign with real event stream -------------------------
        campaign = await make_campaign(session, channel="WHATSAPP", name="WA blast")
        r1 = await make_recipient(session, campaign.id, status="DELIVERED",
                                  lead=converted, sent_at=days_ago(2, 12),
                                  delivered_at=days_ago(2, 13))
        r2 = await make_recipient(session, campaign.id, status="REPLIED",
                                  lead=interested, sent_at=days_ago(2, 12),
                                  delivered_at=days_ago(2, 13),
                                  replied_at=days_ago(2, 14))
        r3 = await make_recipient(session, campaign.id, status="FAILED",
                                  lead=contacted, sent_at=days_ago(2, 12))
        await make_event(session, campaign.id, EventType.MESSAGE_SENT, recipient_id=r1.id)
        await make_event(session, campaign.id, EventType.MESSAGE_SENT, recipient_id=r2.id)
        await make_event(session, campaign.id, EventType.MESSAGE_SENT, recipient_id=r3.id)
        await make_event(session, campaign.id, EventType.MESSAGE_DELIVERED, recipient_id=r1.id)
        await make_event(session, campaign.id, EventType.MESSAGE_DELIVERED, recipient_id=r2.id)
        await make_event(session, campaign.id, EventType.MESSAGE_FAILED, recipient_id=r3.id)
        await make_event(session, campaign.id, EventType.MESSAGE_REPLIED, recipient_id=r2.id)
        await session.flush()

        # --- email campaign with bounces + tracking events ---------------------
        email_campaign = await make_campaign(session, channel="EMAIL", name="Newsletter")
        e1 = await make_recipient(session, email_campaign.id, status="DELIVERED",
                                  address="a@example.com", sent_at=days_ago(1, 9),
                                  delivered_at=days_ago(1, 10),
                                  opened_at=days_ago(1, 12))
        e2 = await make_recipient(session, email_campaign.id, status="FAILED",
                                  address="b@example.com", sent_at=days_ago(1, 9),
                                  bounced_at=days_ago(1, 11))
        await make_event(session, email_campaign.id, EventType.MESSAGE_SENT,
                         recipient_id=e1.id, provider="smtp")
        await make_event(session, email_campaign.id, EventType.MESSAGE_DELIVERED,
                         recipient_id=e1.id, provider="smtp")
        await make_event(session, email_campaign.id, EventType.MESSAGE_BOUNCED,
                         recipient_id=e2.id, provider="smtp",
                         metadata={"bounce_type": "HARD_BOUNCE"})
        await make_event(session, email_campaign.id, EventType.MESSAGE_OPENED,
                         recipient_id=e1.id, provider="smtp")
        await make_event(session, email_campaign.id, EventType.MESSAGE_UNSUBSCRIBED,
                         recipient_id=e1.id, provider="smtp")

        # --- conversations -------------------------------------------------------
        await make_conversation_with_messages(
            session, status="OPEN", created_at=days_ago(2),
            assigned_user=operator.id, lead=fresh,
        )
        await make_conversation_with_messages(
            session, status="RESOLVED", created_at=days_ago(4), closed_at=days_ago(3),
            channel="EMAIL", contact_phone=None,
        )
        await session.flush()

        # --- automation -----------------------------------------------------------
        workflow = Workflow(name="Ping new leads", status="ACTIVE",
                            trigger_type="LEAD_CREATED")
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            workflow_id=workflow.id, version=1, definition={"nodes": []},
            checksum="deadbeef", status="PUBLISHED",
        )
        session.add(version)
        await session.flush()
        await make_workflow_execution(session, workflow, version, status="COMPLETED",
                                      created_at=days_ago(1))
        await make_workflow_execution(session, workflow, version, status="FAILED",
                                      created_at=days_ago(2),
                                      error="PROVIDER_TIMEOUT\nprovider call failed")
        await session.commit()

    return {
        "operator_id": operator.id,
        "campaign_id": campaign.id,
        "email_campaign_id": email_campaign.id,
        "workflow_id": workflow.id,
    }
