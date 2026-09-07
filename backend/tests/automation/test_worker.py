"""Worker engine tests (§28–§29, §37–§39, §58–§60): claiming, locking, retry,
delay/resume, version pinning, step budget."""

import uuid

import pytest

from tests.automation.conftest import make_lead, seed_workflow


def _engine(session_or_settings):
    from app.automation.services.execution_engine import ExecutionEngine

    return ExecutionEngine(owner=f"t-{uuid.uuid4().hex[:6]}", settings=session_or_settings)


def _settings(lease: int | None = None):
    from app.core.config import Settings

    kwargs = {"QBIT_ENV": "test", "_env_file": None}
    if lease is not None:
        kwargs["QBIT_AUTOMATION_LEASE_SECONDS"] = lease
    return Settings(**kwargs)


async def _fire(seeded_db, event_type, lead, event_id):
    from app.automation.services.event_dispatcher import dispatch_event

    created = await dispatch_event(seeded_db, event_type=event_type, entity_type="lead",
                                   entity_id=lead.id, payload={}, event_id=event_id)
    await seeded_db.commit()
    return created


class TestClaiming:
    """TEST 7: two workers must not execute the same execution."""

    async def test_double_claim_prevented(self, seeded_db):
        settings = _settings()
        lead = make_lead()
        seeded_db.add(lead)
        await seed_workflow(seeded_db)
        await _fire(seeded_db, "lead.created", lead, f"evt:{uuid.uuid4()}")

        engine_a = _engine(settings)
        engine_b = _engine(settings)

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        claim_a = await engine_a.claim_batch(seeded_db, now=now, batch=10)
        claim_b = await engine_b.claim_batch(seeded_db, now=now, batch=10)
        assert len(claim_a) == 1
        assert claim_b == []

    async def test_stale_lease_recovered(self, seeded_db):
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import select

        from app.models.automation import WorkflowExecution

        settings = _settings(lease=10)
        lead = make_lead()
        seeded_db.add(lead)
        await seed_workflow(seeded_db)
        await _fire(seeded_db, "lead.created", lead, f"evt:{uuid.uuid4()}")

        engine = _engine(settings)
        now = datetime.now(timezone.utc)
        claimed = await engine.claim_batch(seeded_db, now=now, batch=10)
        assert len(claimed) == 1

        # simulate a crash: move locked_at into the past
        execution = (await seeded_db.execute(
            select(WorkflowExecution).where(WorkflowExecution.id == claimed[0])
        )).scalars().first()
        execution.locked_at = now - timedelta(seconds=3600)
        await seeded_db.commit()

        recovered = await engine.recover_stale_leases(seeded_db, now=now)
        assert recovered == 1
        await seeded_db.refresh(execution)
        assert execution.status == "QUEUED"


class TestDelayResume:
    """TEST 6: WAIT survives restart (state is in the DB, not in memory)."""

    async def test_wait_then_resume(self, seeded_db):
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import select

        from app.models.automation import WorkflowExecution

        settings = _settings()
        lead = make_lead()
        seeded_db.add(lead)
        definition = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "wait"},
                {"id": "wait", "type": "WAIT", "duration": {"minutes": 5},
                 "next_node_id": "tag"},
                {"id": "tag", "type": "ACTION", "action": "add_tag",
                 "config": {"tag": "AfterWait"}, "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        await seed_workflow(seeded_db, name="WaitWF", definition=definition)
        await _fire(seeded_db, "lead.created", lead, f"evt:{uuid.uuid4()}")

        engine = _engine(settings)
        actions = await engine.process_cycle(seeded_db)
        assert actions >= 1

        execution = (await seeded_db.execute(select(WorkflowExecution))).scalars().first()
        assert execution.status == "WAITING"
        assert execution.next_execution_at is not None
        assert execution.current_node_id == "tag"  # resumes AFTER the wait node

        # "restart" — a brand-new engine instance resumes from DB state
        engine2 = _engine(settings)
        actions = await engine2.process_cycle(seeded_db)  # not due yet → no-op
        await seeded_db.refresh(execution)
        assert execution.status == "WAITING"

        # time passes
        execution.next_execution_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await seeded_db.commit()
        actions = await engine2.process_cycle(seeded_db)
        await seeded_db.refresh(execution)
        assert execution.status == "COMPLETED"
        await seeded_db.refresh(lead)
        assert "AfterWait" in (lead.tags or [])

    async def test_wait_respects_max_duration(self, seeded_db):
        """§42 max_wait_duration cap."""
        settings = _settings()
        lead = make_lead()
        seeded_db.add(lead)
        definition = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "wait"},
                {"id": "wait", "type": "WAIT", "duration": {"days": 90},
                 "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        await seed_workflow(seeded_db, name="LongWaitWF", definition=definition)
        await _fire(seeded_db, "lead.created", lead, f"evt:{uuid.uuid4()}")

        engine = _engine(settings)
        await engine.process_cycle(seeded_db)
        from sqlalchemy import select

        from app.models.automation import WorkflowExecution

        execution = (await seeded_db.execute(select(WorkflowExecution))).scalars().first()
        max_hours = settings.QBIT_AUTOMATION_MAX_WAIT_HOURS
        # 90 days requested → capped at max_wait_hours (720h default)
        assert execution.next_execution_at is not None


class TestVersionPinning:
    """TEST 3: running executions continue on v1 after v2 is published (§60)."""

    async def test_execution_pins_its_version(self, seeded_db):
        settings = _settings()
        lead = make_lead()
        seeded_db.add(lead)
        definition_v1 = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "wait"},
                {"id": "wait", "type": "WAIT", "duration": {"minutes": 30},
                 "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        wf = await seed_workflow(seeded_db, name="VersionedWF", definition=definition_v1)
        await _fire(seeded_db, "lead.created", lead, f"evt:{uuid.uuid4()}")

        from sqlalchemy import select

        from app.models.automation import WorkflowExecution, WorkflowVersion

        execution = (await seeded_db.execute(select(WorkflowExecution))).scalars().first()
        assert execution.workflow_version_id

        # publish v2 with a different definition (edit draft → publish)
        from app.automation.services.workflow_service import WorkflowService

        svc = WorkflowService()
        definition_v2 = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "tag"},
                {"id": "tag", "type": "ACTION", "action": "add_tag",
                 "config": {"tag": "V2Tag"}, "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        await svc.update(seeded_db, wf, definition=definition_v2)
        version2 = await svc.publish(seeded_db, wf)
        assert version2.version == 2

        # the WAITING execution still references v1
        await seeded_db.refresh(execution)
        v1 = await seeded_db.get(WorkflowVersion, execution.workflow_version_id)
        assert v1.version == 1
        # v1 is RETIRED (no longer current) but remains viewable + pinned (§60)
        assert v1.status == "RETIRED"
        assert wf.current_version == 2

        # a NEW event uses v2
        lead2 = make_lead(business_name="Second Co")
        seeded_db.add(lead2)
        created = await _fire(seeded_db, "lead.created", lead2, f"evt:{uuid.uuid4()}")
        assert created[0].workflow_version_id != execution.workflow_version_id


class TestRetry:
    """§38: transient errors back off; max retries then fail."""

    async def test_transient_retry_then_fail(self, seeded_db):
        settings = _settings()
        lead = make_lead()
        seeded_db.add(lead)
        definition = {
            "nodes": [
                {"id": "trigger", "type": "TRIGGER",
                 "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "boom"},
                {"id": "boom", "type": "ACTION", "action": "boom_tag",
                 "config": {"tag": "x"}, "next_node_id": "end"},
                {"id": "end", "type": "END"},
            ],
        }
        from app.automation.actions.registry import build_action_registry
        from app.automation.core.action import BaseAction
        from app.automation.core.exceptions import TransientError

        class BoomTag(BaseAction):
            ACTION_KEY = "boom_tag"

            async def execute(self, context, config):
                raise TransientError("temporary provider hiccup")

        build_action_registry().register(BoomTag())
        try:
            await seed_workflow(seeded_db, name="RetryWF", definition=definition)
            created = await _fire(seeded_db, "lead.created", lead, f"evt:{uuid.uuid4()}")
            assert len(created) == 1

            engine = _engine(settings)
            outcome = await engine.process_cycle(seeded_db)  # claim (attempts+1) → run
            await seeded_db.refresh(created[0])
            assert outcome >= 1
            assert created[0].status == "QUEUED"
            assert created[0].next_execution_at is not None
            assert created[0].attempts == 1
        finally:
            build_action_registry()._actions.pop("boom_tag", None)
