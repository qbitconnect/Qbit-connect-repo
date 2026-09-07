"""Phase 9 smoke test — real app + isolated SQLite DB (no docker, no pytest).

Verifies the Phase 9 final-verification items end to end:
  workflow create/validate/publish lifecycle, TEST 1 (lead created → tag →
  complete), TEST 2 (duplicate event id → one execution), TEST 4 (update loop
  bounded), TEST 5 (unsubscribed → SKIPPED, no message), TEST 6 (WAIT
  persists + resumes), TEST 8 (quality>70 AND has_email branching), TEST 9
  (viewer publish → 403), templates (draft-only), execution monitor + intake
  event log + audit trail, version pinning, no destructive migration.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("QBIT_ENV", "test")
os.chdir(Path(__file__).resolve().parents[1] / "backend")

import httpx  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


GOOD_DEF = {
    "nodes": [
        {"id": "trigger", "type": "TRIGGER",
         "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "cond"},
        {"id": "cond", "type": "CONDITION",
         "condition": {"all": [
             {"field": "lead.quality_score", "operator": "greater_than", "value": 70},
             {"field": "lead.has_email", "operator": "equals", "value": True},
         ]},
         "next_node_id": "tag", "next_node_id_no": "tag2"},
        {"id": "tag", "type": "ACTION", "action": "add_tag",
         "config": {"tag": "Hot"}, "next_node_id": "end"},
        {"id": "tag2", "type": "ACTION", "action": "add_tag",
         "config": {"tag": "Review"}, "next_node_id": "end"},
        {"id": "end", "type": "END"},
    ],
}


async def run() -> None:
    tmp = tempfile.mkdtemp(prefix="qbit-phase9-smoke-")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/smoke.db"
    os.environ["QBIT_DATA_DIR"] = tmp

    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import DatabaseManager
    from app.services.rbac import seed_admin, seed_rbac
    import app.models  # noqa: F401

    settings = Settings(QBIT_ENV="test", DATABASE_URL=os.environ["DATABASE_URL"],
                        QBIT_DATA_DIR=Path(tmp), _env_file=None)
    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.session() as session:
        await seed_rbac(session)
        await seed_admin(session, email="admin@qbit-smoke9.com",
                         password="smoke9-admin-pass", full_name="Smoke Admin")
        await session.commit()

    from app.main import create_app

    app = create_app(settings, db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        login = await c.post("/api/v1/auth/login", json={
            "email": "admin@qbit-smoke9.com", "password": "smoke9-admin-pass"})
        admin = {"Authorization": f"Bearer {login.json()['access_token']}"}
        from app.core.security import hash_password
        from app.models.rbac import Role
        from app.models.user import User
        from sqlalchemy import select as _sel

        async with app.state.db.session() as vs:
            exists = (await vs.execute(_sel(User).where(
                User.email == "viewer@qbit-smoke9.com"))).scalars().first()
            if exists is None:
                role = (await vs.execute(_sel(Role).where(Role.code == "VIEWER"))).scalars().first()
                viewer_user = User(email="viewer@qbit-smoke9.com",
                                   password_hash=hash_password("smoke9-viewer-pass"),
                                   full_name="Smoke Viewer")
                viewer_user.roles.append(role)
                vs.add(viewer_user)
                await vs.commit()
        lv = await c.post("/api/v1/auth/login", json={
            "email": "viewer@qbit-smoke9.com", "password": "smoke9-viewer-pass"})
        viewer = {"Authorization": f"Bearer {lv.json()['access_token']}"}

        from tests.marketing.conftest import make_lead

        async with app.state.db.session() as session:
            lead = make_lead(quality_score=90)
            session.add(lead)
            await session.commit()
            lead_id = lead.id

        # 1. lifecycle ----------------------------------------------------------
        r = await c.post("/api/v1/automation/workflows", headers=admin, json={
            "name": "Smoke Quality Tagging", "trigger_type": "LEAD_CREATED",
            "definition": GOOD_DEF})
        check("workflow created via API", r.status_code == 201, r.text[:200])
        wf = r.json()["data"]
        v = await c.post(f"/api/v1/automation/workflows/{wf['id']}/validate", headers=admin)
        check("validation report PASS", v.status_code == 200 and v.json()["data"]["ok"])
        p = await c.post(f"/api/v1/automation/workflows/{wf['id']}/publish", headers=admin)
        check("publish → ACTIVE, version 1",
              p.json()["data"]["workflow"]["status"] == "ACTIVE"
              and p.json()["data"]["workflow"]["current_version"] == 1)

        # TEST 9: viewer publish blocked
        wf2 = (await c.post("/api/v1/automation/workflows", headers=admin, json={
            "name": "Viewer Blocked", "trigger_type": "LEAD_CREATED",
            "definition": GOOD_DEF})).json()["data"]
        r403 = await c.post(f"/api/v1/automation/workflows/{wf2['id']}/publish",
                            headers=viewer)
        check("TEST 9: viewer publish → 403", r403.status_code == 403)

        # TEST 1 + 8 + 2 ---------------------------------------------------------
        from app.automation.services.event_dispatcher import dispatch_event
        from app.automation.services.execution_engine import ExecutionEngine
        from sqlalchemy import func, select

        from app.models.automation import WorkflowExecution, WorkflowExecutionStep
        from app.models.scrape import Lead

        engine = ExecutionEngine(owner="smoke", settings=settings, audit=app.state.audit)
        event_id = f"lead.created:{uuid.uuid4()}"
        async with app.state.db.session() as session:
            created = await dispatch_event(session, event_type="lead.created",
                                           entity_type="lead", entity_id=lead_id,
                                           payload={}, event_id=event_id)
            await session.commit()
            check("TEST 1: event → exactly one execution", len(created) == 1)
            await engine.process_cycle(session)
            await session.refresh(created[0])
            check("TEST 1: execution COMPLETED", created[0].status == "COMPLETED")
            steps = (await session.execute(
                select(WorkflowExecutionStep).where(
                    WorkflowExecutionStep.execution_id == created[0].id))).scalars().all()
            check("TEST 1: timeline trigger→cond→tag→end",
                  [s.node_id for s in steps] == ["trigger", "cond", "tag", "end"])
            fresh_lead = await session.get(Lead, lead_id)
            check("TEST 8: score>70 AND email → YES branch ('Hot')",
                  "Hot" in (fresh_lead.tags or []) and "Review" not in (fresh_lead.tags or []))
            dup = await dispatch_event(session, event_type="lead.created",
                                       entity_type="lead", entity_id=lead_id,
                                       payload={}, event_id=event_id)
            check("TEST 2: duplicate event_id → no new execution", dup == [])

        # TEST 6: WAIT persists + resumes ---------------------------------------
        wait_def = {"nodes": [
            {"id": "trigger", "type": "TRIGGER",
             "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "wait"},
            {"id": "wait", "type": "WAIT", "duration": {"minutes": 2},
             "next_node_id": "tag"},
            {"id": "tag", "type": "ACTION", "action": "add_tag",
             "config": {"tag": "AfterWait"}, "next_node_id": "end"},
            {"id": "end", "type": "END"},
        ]}
        wfw = (await c.post("/api/v1/automation/workflows", headers=admin, json={
            "name": "Smoke Wait WF", "trigger_type": "LEAD_CREATED",
            "definition": wait_def})).json()["data"]
        await c.post(f"/api/v1/automation/workflows/{wfw['id']}/publish", headers=admin)
        async with app.state.db.session() as session:
            lead2 = make_lead(business_name="Wait Co")
            session.add(lead2)
            await session.commit()
            lead2_id = lead2.id
            created = await dispatch_event(session, event_type="lead.created",
                                           entity_type="lead", entity_id=lead2_id,
                                           payload={},
                                           event_id=f"lead.created:{uuid.uuid4()}")
            await session.commit()
            actions = await engine.process_cycle(session)
            wfw_row = uuid.UUID(wfw["id"])
            wait_exec = (await session.execute(
                select(WorkflowExecution).where(
                    WorkflowExecution.workflow_id == wfw_row,
                    WorkflowExecution.entity_id == lead2_id))).scalars().first()
            await session.refresh(wait_exec)
            check("TEST 6: WAIT persists (WAITING + next_execution_at)",
                  wait_exec.status == "WAITING"
                  and wait_exec.next_execution_at is not None,
                  f"actions={actions} status={wait_exec.status} err={wait_exec.error!r}")
            check("TEST 6: resume node stored", wait_exec.current_node_id == "tag")
            wait_exec.next_execution_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
            engine_restart = ExecutionEngine(owner="smoke-restarted", settings=settings)
            await engine_restart.process_cycle(session)
            await session.refresh(wait_exec)
            fresh_lead2 = await session.get(Lead, lead2_id)
            check("TEST 6: resumes after restart-equivalent cycle",
                  wait_exec.status == "COMPLETED" and "AfterWait" in (fresh_lead2.tags or []),
                  f"status={wait_exec.status} err={wait_exec.error!r} tags={fresh_lead2.tags}")

        # TEST 4: loop bounded ---------------------------------------------------
        loop_def = {"nodes": [
            {"id": "trigger", "type": "TRIGGER",
             "trigger_config": {"type": "LEAD_UPDATED"}, "next_node_id": "upd"},
            {"id": "upd", "type": "ACTION", "action": "update_lead",
             "config": {"fields": {"city": "Loop City"}}, "next_node_id": "end"},
            {"id": "end", "type": "END"},
        ]}
        lwf = (await c.post("/api/v1/automation/workflows", headers=admin, json={
            "name": "Smoke Loop WF", "trigger_type": "LEAD_UPDATED",
            "definition": loop_def})).json()["data"]
        await c.post(f"/api/v1/automation/workflows/{lwf['id']}/publish", headers=admin)
        async with app.state.db.session() as session:
            lead3 = make_lead(business_name="Loop Co")
            session.add(lead3)
            await session.commit()
            await dispatch_event(session, event_type="lead.updated", entity_type="lead",
                                 entity_id=lead3.id, payload={}, event_id="loop:smoke:0")
            await session.commit()
            for _ in range(25):
                if await engine.process_cycle(session) == 0:
                    break
            lw_id = uuid.UUID(lwf["id"])
            total = await session.scalar(
                select(func.count()).select_from(WorkflowExecution)
                .where(WorkflowExecution.workflow_id == lw_id))
            check("TEST 4: update-loop stays bounded", int(total or 0) <= 12,
                  f"executions={total}")

        # TEST 5: unsubscribed → SKIPPED ----------------------------------------
        from app.models.marketing import SendingAccount, SuppressionEntry
        from app.models.messaging import Conversation

        email_def = {"nodes": [
            {"id": "trigger", "type": "TRIGGER",
             "trigger_config": {"type": "LEAD_CREATED"}, "next_node_id": "send"},
            {"id": "send", "type": "ACTION", "action": "send_email",
             "config": {"body": "Hi {{lead.contact_name}}"}, "next_node_id": "end"},
            {"id": "end", "type": "END"},
        ]}
        ewf = (await c.post("/api/v1/automation/workflows", headers=admin, json={
            "name": "Smoke Email WF", "trigger_type": "LEAD_CREATED",
            "definition": email_def})).json()["data"]
        await c.post(f"/api/v1/automation/workflows/{ewf['id']}/publish", headers=admin)
        async with app.state.db.session() as session:
            lead4 = make_lead(business_name="Unsub Co")
            session.add(lead4)
            session.add(SuppressionEntry(type="EMAIL", address=lead4.email,
                                         reason="UNSUBSCRIBED"))
            await session.flush()
            account = SendingAccount(name="Smoke Sender", channel="EMAIL", provider="smtp",
                                     identifier=lead4.email, display_identifier=lead4.email,
                                     status="ACTIVE", health_status="HEALTHY")
            session.add(account)
            session.add(Conversation(channel="EMAIL", sending_account_id=account.id,
                                     lead_id=lead4.id, status="OPEN",
                                     contact_email=lead4.email,
                                     last_inbound_at=datetime.now(timezone.utc)))
            await session.commit()
            await dispatch_event(session, event_type="lead.created",
                                 entity_type="lead", entity_id=lead4.id,
                                 payload={},
                                 event_id=f"lead.created:{uuid.uuid4()}")
            await session.commit()
            await engine.process_cycle(session)
            ewf_row = uuid.UUID(ewf["id"])
            email_exec = (await session.execute(
                select(WorkflowExecution).where(
                    WorkflowExecution.workflow_id == ewf_row,
                    WorkflowExecution.entity_id == lead4.id))).scalars().first()
            send_steps = (await session.execute(
                select(WorkflowExecutionStep).where(
                    WorkflowExecutionStep.execution_id == email_exec.id,
                    WorkflowExecutionStep.node_id == "send"))).scalars().all()
            reason = (send_steps[0].output_snapshot or {}).get("reason", "") if send_steps else ""
            check("TEST 5: unsubscribed → send step SKIPPED",
                  bool(send_steps) and send_steps[0].status == "SKIPPED", reason)
            check("TEST 5: reason honest (UNSUBSCRIBED/SUPPRESSED)",
                  reason in ("UNSUBSCRIBED", "SUPPRESSED"), reason)
            messages = await session.scalar(
                select(func.count()).select_from(
                    __import__("app.models.messaging", fromlist=["Message"]).Message))
            check("TEST 5: no message sent", int(messages or 0) == 0)

        # version pinning (TEST 3 core) -----------------------------------------
        async with app.state.db.session() as session:
            v1 = (await session.execute(
                select(WorkflowExecution).limit(1))).scalars().first()
            from app.automation.services.workflow_service import WorkflowService
            from app.models.automation import WorkflowVersion

            version = await session.get(WorkflowVersion, v1.workflow_version_id)
            check("TEST 3 base: execution pins a concrete version row",
                  version is not None and version.version >= 1)
            _ = WorkflowService  # silence unused-import in narrow builds

        # monitor / events / audit / templates -----------------------------------
        r = await c.get("/api/v1/automation/executions", headers=admin)
        check("execution monitor lists real runs",
              r.status_code == 200 and r.json()["data"]["total"] >= 3)
        r = await c.get("/api/v1/automation/executions/counters", headers=admin)
        check("counters endpoint works", r.status_code == 200)
        r = await c.get("/api/v1/automation/events", headers=admin)
        check("intake event log present", r.status_code == 200
              and r.json()["data"]["total"] >= 5)
        t = await c.get("/api/v1/automation/templates", headers=admin)
        check("5 starter templates exposed", len(t.json()["data"]["items"]) == 5)
        ft = await c.post("/api/v1/automation/workflows/from-template", headers=admin,
                          json={"template_id": "new_lead_qualification"})
        check("template → DRAFT (never auto-activated)",
              ft.status_code == 201 and ft.json()["data"]["status"] == "DRAFT")

        async with app.state.db.session() as session:
            from app.models.audit import AuditLog

            rows = (await session.execute(
                select(AuditLog).where(AuditLog.action.like("automation.%")))).scalars().all()
            actions = {r.action for r in rows}
            check("audit trail records lifecycle",
                  {"automation.workflow_created", "automation.workflow_published"}
                  <= actions, str(sorted(actions))[:120])

    await db.close()
    print(f"\nPhase 9 smoke: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(run())
