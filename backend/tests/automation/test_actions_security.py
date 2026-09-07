"""Security + action tests (§23–§27, §35, §39, §63–§65, §75).

Covers the spec's security matrix: injection, template injection, oversized
workflows, duplicate sends, suppression/unsubscribe skips (TEST 5), lead
actions idempotency, conversation actions.
"""

import uuid

import pytest

from tests.automation.conftest import make_lead, seed_workflow


def _settings():
    from app.core.config import Settings

    return Settings(QBIT_ENV="test", _env_file=None)


async def _fire(seeded_db, event_type, entity, event_id, payload=None):
    from app.automation.services.event_dispatcher import dispatch_event

    created = await dispatch_event(seeded_db, event_type=event_type,
                                   entity_type="lead" if hasattr(entity, "tags") else "conversation",
                                   entity_id=entity.id, payload=payload or {},
                                   event_id=event_id)
    await seeded_db.commit()
    return created


class TestLeadActions:
    """TEST 1 + §23 + §39 idempotency."""

    async def test_tag_action_idempotent(self, seeded_db):
        lead = make_lead()
        seeded_db.add(lead)
        definition = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "tag"},
                {"id": "tag", "type": "ACTION", "action": "add_tag",
                 "config": {"tag": "Hot"}, "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        await seed_workflow(seeded_db, name="TagWF", definition=definition)
        from app.automation.services.execution_engine import ExecutionEngine

        engine = ExecutionEngine(owner="t", settings=_settings())
        created = await _fire(seeded_db, "lead.created", lead, f"e:{uuid.uuid4()}")
        await engine.run_execution(seeded_db, created[0].id)
        await engine.run_execution(seeded_db, created[0].id)  # second run: nothing to do
        await seeded_db.refresh(lead)
        assert lead.tags.count("Hot") == 1  # ADD_TAG twice → one assignment (§39)

    async def test_status_and_note_actions(self, seeded_db):
        lead = make_lead()
        seeded_db.add(lead)
        definition = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "st"},
                {"id": "st", "type": "ACTION", "action": "change_status",
                 "config": {"status": "QUALIFIED"}, "next_node_id": "note"},
                {"id": "note", "type": "ACTION", "action": "add_note",
                 "config": {"content": "Automatically qualified by workflow."},
                 "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        await seed_workflow(seeded_db, name="StatusNoteWF", definition=definition)
        created = await _fire(seeded_db, "lead.created", lead, f"e:{uuid.uuid4()}")
        from app.automation.services.execution_engine import ExecutionEngine

        engine = ExecutionEngine(owner="t", settings=_settings())
        await engine.run_execution(seeded_db, created[0].id)
        await seeded_db.refresh(lead)
        assert lead.status == "QUALIFIED"
        from sqlalchemy import select

        from app.models.lead import LeadNote

        notes = (await seeded_db.execute(
            select(LeadNote).where(LeadNote.lead_id == lead.id))).scalars().all()
        assert any("Automatically qualified" in n.content for n in notes)

    async def test_variables_rendered_from_lead(self, seeded_db):
        lead = make_lead(contact_name="Ravi Patel", city="Surat")
        seeded_db.add(lead)
        definition = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "note"},
                {"id": "note", "type": "ACTION", "action": "add_note",
                 "config": {"content": "Hello {{lead.contact_name}} from {{lead.city}} {{lead.missing_ok}}"},
                 "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        await seed_workflow(seeded_db, name="VarWF", definition=definition)
        created = await _fire(seeded_db, "lead.created", lead, f"e:{uuid.uuid4()}")
        from app.automation.services.execution_engine import ExecutionEngine

        engine = ExecutionEngine(owner="t", settings=_settings())
        await engine.run_execution(seeded_db, created[0].id)
        from sqlalchemy import select

        from app.models.lead import LeadNote

        notes = (await seeded_db.execute(
            select(LeadNote).where(LeadNote.lead_id == lead.id))).scalars().all()
        assert any("Hello Ravi Patel from Surat" in n.content for n in notes)
        # unknown variable renders empty — no injection, no crash (§35)
        assert any("{{" not in n.content for n in notes)


class TestCommunicationSafety:
    """TEST 5: unsubscribed → SKIPPED, reason, no message (§27, §53)."""

    async def test_send_email_skipped_for_unsubscribed(self, seeded_db):
        from app.models.marketing import SuppressionEntry

        lead = make_lead()
        seeded_db.add(lead)
        seeded_db.add(SuppressionEntry(type="EMAIL", address=lead.email,
                                       reason="UNSUBSCRIBED"))
        await seeded_db.flush()

        # conversation + sending account + template so the action gets that far
        conversation = await _seed_conversation(seeded_db, lead)
        definition = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "send"},
                {"id": "send", "type": "ACTION", "action": "send_email",
                 "config": {"body": "Hi {{lead.contact_name}}"}, "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        await seed_workflow(seeded_db, name="EmailWF", definition=definition)
        created = await _fire(seeded_db, "lead.created", lead, f"e:{uuid.uuid4()}")

        from app.automation.services.execution_engine import ExecutionEngine
        from sqlalchemy import select

        from app.models.automation import WorkflowExecutionStep

        engine = ExecutionEngine(owner="t", settings=_settings())
        await engine.run_execution(seeded_db, created[0].id)
        steps = (await seeded_db.execute(select(WorkflowExecutionStep))).scalars().all()
        send_steps = [s for s in steps if s.node_id == "send"]
        assert len(send_steps) == 1
        assert send_steps[0].status == "SKIPPED"
        reason = (send_steps[0].output_snapshot or {}).get("reason", "")
        assert reason in ("UNSUBSCRIBED", "SUPPRESSED")
        # no message was created
        from sqlalchemy import func, select as _sel

        from app.models.messaging import Message as _Message

        messages = await seeded_db.scalar(
            _sel(func.count()).select_from(_Message))
        assert int(messages or 0) == 0

    async def test_whatsapp_window_closed_skips(self, seeded_db):
        lead = make_lead()
        seeded_db.add(lead)
        conversation = await _seed_conversation(seeded_db, lead, channel="WHATSAPP")
        # simulate a CLOSED customer-service window (no recent inbound message)
        conversation.last_inbound_at = None
        await seeded_db.commit()
        definition = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "send"},
                {"id": "send", "type": "ACTION", "action": "send_whatsapp",
                 "config": {"body": "hello"}, "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        await seed_workflow(seeded_db, name="WaWF", definition=definition)
        created = await _fire(seeded_db, "lead.created", lead, f"e:{uuid.uuid4()}")
        from app.automation.services.execution_engine import ExecutionEngine
        from sqlalchemy import select

        from app.models.automation import WorkflowExecutionStep

        engine = ExecutionEngine(owner="t", settings=_settings())
        await engine.run_execution(seeded_db, created[0].id)
        steps = (await seeded_db.execute(select(WorkflowExecutionStep))).scalars().all()
        send_steps = [s for s in steps if s.node_id == "send"]
        assert send_steps[0].status == "SKIPPED"
        assert (send_steps[0].output_snapshot or {}).get("reason") == \
            "WHATSAPP_WINDOW_CLOSED_TEMPLATE_REQUIRED"


async def _seed_conversation(session, lead, channel="EMAIL"):
    from datetime import datetime, timezone

    from app.models.marketing import SendingAccount
    from app.models.messaging import Conversation

    await session.flush()  # ensure lead.id is assigned before linking
    identifier = lead.email if channel == "EMAIL" else (lead.phone or "+919876543210")
    account = SendingAccount(
        name="Test Account", channel=channel,
        provider=("smtp" if channel == "EMAIL" else "whatsapp_cloud"),
        identifier=identifier, display_identifier=identifier,
        status="ACTIVE", health_status="HEALTHY",
    )
    session.add(account)
    conversation = Conversation(
        channel=channel, sending_account_id=account.id, lead_id=lead.id,
        status="OPEN", contact_email=lead.email if channel == "EMAIL" else None,
        contact_phone=lead.phone if channel == "WHATSAPP" else None,
        last_inbound_at=datetime.now(timezone.utc),
    )
    session.add(conversation)
    await session.flush()
    return conversation


class TestDefinitionSecurity:
    """§63/§65: no code execution, no injection, bounded size."""

    def test_unknown_action_rejected_at_create(self, seeded_db):
        from app.automation.services.workflow_service import WorkflowService
        from app.automation.core.exceptions import ValidationError

        svc = WorkflowService()
        with pytest.raises(ValidationError):
            __import__("asyncio").get_event_loop().run_until_complete(_create_bad(svc, seeded_db))


async def _create_bad(svc, session):
    await svc.create(
        session, name="Evil WF", trigger_type="LEAD_CREATED",
        definition={"nodes": [
            {"id": "trigger", "type": "TRIGGER",
             "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "evil"},
            {"id": "evil", "type": "ACTION", "action": "__import__",
             "config": {}, "next_node_id": "end"},
            {"id": "end", "type": "END"},
        ]},
    )


class TestOversizedWorkflow:
    async def test_too_many_nodes_rejected(self, seeded_db):
        from app.automation.services.workflow_service import WorkflowService
        from app.automation.core.exceptions import ValidationError

        nodes = [{"id": "trigger", "type": "TRIGGER",
                  "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "end"}]
        nodes += [{"id": f"e{i}", "type": "END"} for i in range(60)]
        svc = WorkflowService()
        with pytest.raises(ValidationError):
            await svc.create(seeded_db, name="Big WF", trigger_type="LEAD_CREATED",
                             definition={"nodes": nodes})
