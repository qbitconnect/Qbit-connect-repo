"""Phase 9 automation test fixtures — reuses the marketing fixture stack."""

from tests.marketing.conftest import (  # noqa: F401
    admin_headers,
    client,
    make_lead,
    seeded_db,
    seed_leads,
    viewer_headers,
)


async def seed_workflow(session, *, name="Test WF", trigger_type="LEAD_CREATED",
                        definition=None, status=None, publish=True):
    """Create (+ optionally publish) a workflow via the service layer."""
    from app.automation.services.workflow_service import WorkflowService
    from app.models.automation import Workflow, WorkflowStatus
    from sqlalchemy import select

    if definition is None:
        definition = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": trigger_type}, "next_node_id": "tag"},
                {"id": "tag", "type": "ACTION", "action": "add_tag",
                 "config": {"tag": "Auto"}, "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
    svc = WorkflowService()
    wf = await svc.create(session, name=name, trigger_type=trigger_type,
                          definition=definition)
    if publish:
        await svc.publish(session, wf)
    if status is not None:
        wf.status = status
        await session.commit()
        await session.refresh(wf)
    return (await session.execute(
        select(Workflow).where(Workflow.name == name).limit(1)
    )).scalars().first()
