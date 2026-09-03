"""Phase 6 smoke test — real uvicorn server + isolated SQLite DB + tmp storage.

Verifies the final-verification items end to end over HTTP:
  app starts, login works, RBAC works, connections CRUD + validate + health +
  sync-templates + templates, webhook challenge/signature/duplicates, campaign
  launch via whatsapp_mock, delivery events update analytics, secrets never
  exposed, existing lead/scrape/campaign surfaces still respond.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("QBIT_ENV", "test")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

PASS = 0
FAIL = 0
APP_SECRET = "smoke-app-secret-0123456789abcdef"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


async def main() -> int:
    import uvicorn

    tmp = tempfile.mkdtemp(prefix="qbit-phase6-smoke-")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/smoke.db"
    os.environ["QBIT_DATA_DIR"] = f"{tmp}/data"
    os.environ["QBIT_LOG_DIR"] = f"{tmp}/logs"
    os.environ["QBIT_BACKUP_DIR"] = f"{tmp}/backups"

    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import DatabaseManager
    from app.main import create_app
    from app.services import rbac as rbac_service

    settings = Settings(
        QBIT_ENV="test",
        QBIT_SECRET_KEY="smoke-secret-key-" + "z" * 48,
        DATABASE_URL=os.environ["DATABASE_URL"],
        QBIT_DATA_DIR=Path(f"{tmp}/data"),
        QBIT_EXPORT_DIR=Path(f"{tmp}/data/exports"),
        QBIT_LOG_DIR=Path(f"{tmp}/logs"),
        QBIT_BACKUP_DIR=Path(f"{tmp}/backups"),
        WHATSAPP_APP_SECRET=APP_SECRET,
        WHATSAPP_WEBHOOK_VERIFY_TOKEN="smoke-verify-token",
        _env_file=None,
    )
    db = DatabaseManager(settings)
    import app.models  # noqa: F401

    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(settings, db=db)
    async with db.session() as session:
        await rbac_service.seed_rbac(session)
        await rbac_service.seed_admin(
            session, email="admin@qbit-smoke.com", password="SmokePass!123",
            full_name="Smoke Admin",
        )

    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="critical")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    base = "http://127.0.0.1:8765"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as c:
            print("== core ==")
            r = await c.get("/health")
            check("1. app starts + health", r.status_code == 200)
            r = await c.post("/api/v1/auth/login",
                             json={"email": "admin@qbit-smoke.com", "password": "SmokePass!123"})
            token = r.json().get("access_token") if r.status_code == 200 else None
            check("2. existing login works", r.status_code == 200 and bool(token),
                  "" if r.status_code == 200 else f"status={r.status_code} body={r.text[:200]}")
            headers = {"Authorization": f"Bearer {token}"}
            r = await c.get("/api/v1/leads", headers=headers)
            check("5. lead workspace API alive", r.status_code == 200)
            r = await c.get("/api/v1/campaigns", headers=headers)
            check("6. marketing engine alive", r.status_code == 200)
            r = await c.get("/api/v1/scrapers", headers=headers)
            check("4. scraping alive", r.status_code == 200)

            # RBAC: unauthenticated request must be rejected
            r = await c.get("/api/v1/connections")
            check("3. RBAC: unauthenticated rejected", r.status_code == 401)

            print("== connections ==")
            r = await c.post("/api/v1/connections/whatsapp", headers=headers, json={
                "name": "QBIT WhatsApp 01", "provider": "whatsapp_mock",
                "phone_number_id": "111222333", "business_account_id": "999999999999",
                "credentials": {"access_token": "EAAG-smoke-token-00000001"},
            })
            check("7. whatsapp provider configuration works", r.status_code == 201)
            acct1 = r.json()["data"]
            r = await c.post("/api/v1/connections/whatsapp", headers=headers, json={
                "name": "QBIT WhatsApp 02", "provider": "whatsapp_mock",
                "phone_number_id": "444555666",
                "credentials": {"access_token": "EAAG-smoke-token-00000002"},
            })
            acct2 = r.json()["data"]
            check("8. multiple sending accounts work", r.status_code == 201 and acct1["id"] != acct2["id"])
            body = json.dumps(acct1)
            check("9. credentials protected (no token in response)",
                  "EAAG-smoke-token" not in body)

            r = await c.post(f"/api/v1/connections/whatsapp/{acct1['id']}/validate", headers=headers)
            check("10. account validation works (ACTIVE)",
                  r.status_code == 200 and r.json()["data"]["status"] == "ACTIVE")
            r = await c.post(f"/api/v1/connections/whatsapp/{acct1['id']}/health", headers=headers)
            check("11. health checks work",
                  r.status_code == 200 and r.json()["data"]["health_status"] == "HEALTHY")
            r = await c.post(f"/api/v1/connections/whatsapp/{acct1['id']}/sync-templates", headers=headers)
            check("12. templates synchronize", r.status_code == 200 and r.json()["data"]["created"] == 4)
            r = await c.get(f"/api/v1/connections/whatsapp/{acct1['id']}/templates", headers=headers)
            check("13. provider templates listed + approval states",
                  r.status_code == 200 and len(r.json()["data"]["items"]) == 4)

            # UI pages (cookie session)
            await c.post("/login", data={
                "email": "admin@qbit-smoke.com", "password": "SmokePass!123",
                "next": "/connections"}, follow_redirects=False)
            r = await c.get("/connections")
            check("31. connections UI renders", r.status_code == 200 and "CONNECTIONS" in r.text)

            print("== campaign launch (whatsapp_mock) ==")
            from app.models.scrape import Lead

            async with db.session() as session:
                lead = Lead(
                    business_name="Smoke Biz", contact_name="Smoke Contact",
                    email="smoke@biz.test", phone="+919876543210",
                    phone_norm="+919876543210", email_norm="smoke@biz.test",
                    source="smoke", source_type="manual", status="NEW",
                )
                import uuid as uuid_mod

                meta = dict(lead.metadata_json or {})
                meta["marketing_opt_in"] = True
                lead.metadata_json = meta
                session.add(lead)
                await session.commit()
                lead_id = str(lead.id)

                from app.models.marketing import CampaignTemplate

                row = await session.scalar(
                    select(CampaignTemplate).where(
                        CampaignTemplate.provider_template_id == "tpl-approved-1")
                )
                row.variables = ["contact_name", "business_name"]
                await session.commit()
                template_id = str(row.id)

                from app.services.marketing.campaign import CampaignService

                svc = CampaignService()
                campaign = await svc.create(
                    session, name="Smoke WA Campaign", channel="WHATSAPP",
                    audience_definition={"type": "selected", "lead_ids": [lead_id]},
                    template_id=uuid_mod.UUID(template_id),
                    sending_account_id=uuid_mod.UUID(acct1["id"]),
                )
                campaign_id = str(campaign.id)
            r = await c.post(f"/api/v1/campaigns/{campaign_id}/validate", headers=headers)
            check("17. campaign validation passes", r.status_code == 200 and r.json()["data"]["ok"] is True)
            r = await c.post(f"/api/v1/campaigns/{campaign_id}/launch", headers=headers)
            check("18. launch accepted (queue integration)",
                  r.status_code == 200 and r.json()["data"]["status"] in ("QUEUED", "RUNNING"))

            # run the worker cycles in-process
            from app.services.marketing.worker import CampaignWorker

            worker = CampaignWorker(settings, app.state.marketing_providers)
            async with db.session() as session:
                await worker.process_cycle(session)
                await worker.process_cycle(session)
                from app.models.marketing import CampaignRecipient

                recips = (await session.execute(
                    select(CampaignRecipient).where(
                        CampaignRecipient.campaign_id == uuid_mod.UUID(campaign_id))
                )).scalars().all()
                sent = [x for x in recips if x.status == "SENT"]
                check("19/20. provider send works (mock provider)",
                      len(sent) == 1 and sent[0].provider_message_id.startswith("wamid.mock"))
                wamid = sent[0].provider_message_id
                check("21. idempotency: exactly one recipient/queue row",
                      len(recips) == 1)

            print("== webhooks ==")

            def sign(body: bytes) -> str:
                return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()

            payload = {
                "object": "whatsapp_business_account",
                "entry": [{"id": "999", "changes": [{"field": "messages", "value": {
                    "metadata": {"phone_number_id": "111222333"},
                    "contacts": [], "messages": [],
                    "statuses": [{"id": wamid, "status": "delivered",
                                  "timestamp": int(datetime.now(timezone.utc).timestamp()),
                                  "recipient_id": "919876543210"}],
                }}]}],
            }
            raw = json.dumps(payload).encode()
            r = await c.post("/api/v1/webhooks/whatsapp", content=raw,
                             headers={"X-Hub-Signature-256": sign(raw)})
            check("24. delivered event applied", r.status_code == 200 and r.json()["data"]["applied"] == 1)
            r = await c.post("/api/v1/webhooks/whatsapp", content=raw,
                             headers={"X-Hub-Signature-256": sign(raw)})
            check("23. duplicate webhook protection", r.json()["data"]["duplicates"] == 1)
            r = await c.post("/api/v1/webhooks/whatsapp", content=raw,
                             headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
            check("42a. forged webhook rejected", r.status_code == 401)
            r = await c.post("/api/v1/webhooks/whatsapp", content=raw)
            check("42b. missing signature rejected", r.status_code == 401)

            def read_payload(status: str) -> bytes:
                p = {
                    "object": "whatsapp_business_account",
                    "entry": [{"id": "999", "changes": [{"field": "messages", "value": {
                        "metadata": {"phone_number_id": "111222333"},
                        "contacts": [], "messages": [],
                        "statuses": [{"id": wamid, "status": status,
                                      "timestamp": int(datetime.now(timezone.utc).timestamp()),
                                      "recipient_id": "919876543210"}],
                    }}]}],
                }
                return json.dumps(p).encode()

            raw_read = read_payload("read")
            r = await c.post("/api/v1/webhooks/whatsapp", content=raw_read,
                             headers={"X-Hub-Signature-256": sign(raw_read)})
            check("25/26. read event applied", r.status_code == 200 and r.json()["data"]["applied"] == 1)

            # inbound message → conversation
            inb = {
                "object": "whatsapp_business_account",
                "entry": [{"id": "999", "changes": [{"field": "messages", "value": {
                    "metadata": {"phone_number_id": "111222333"},
                    "contacts": [{"profile": {"name": "Smoke"}, "wa_id": "919876543210"}],
                    "messages": [{"from": "919876543210", "id": "wamid.smoke-in-1",
                                  "timestamp": int(datetime.now(timezone.utc).timestamp()),
                                  "type": "text", "text": {"body": "Interested!"}}],
                    "statuses": [],
                }}]}],
            }
            raw_inb = json.dumps(inb).encode()
            r = await c.post("/api/v1/webhooks/whatsapp", content=raw_inb,
                             headers={"X-Hub-Signature-256": sign(raw_inb)})
            check("28. incoming message architecture works",
                  r.status_code == 200 and r.json()["data"]["inbound"] == 1)
            async with db.session() as session:
                from app.models.messaging import Conversation

                conv = (await session.execute(select(Conversation))).scalars().first()
                check("28b. conversation created + lead matched",
                      conv is not None and conv.lead_id is not None)

            r = await c.get(f"/api/v1/campaigns/{campaign_id}/analytics", headers=headers)
            a = r.json()["data"]
            check("29. campaign analytics update correctly",
                  a["messages"]["delivered"] == 1 and a["messages"]["read"] == 1
                  and a["messages"]["replied"] == 1 and a["rates"]["read_rate"] == 1.0)

            from app.models.audit import AuditLog

            async with db.session() as session:
                rows = (await session.execute(select(AuditLog))).scalars().all()
                actions = {row.action for row in rows}
                blob = " ".join(str(getattr(row, "action_metadata", "") or "") for row in rows)
                check("31. audit logs recorded",
                      {"whatsapp_account.created", "whatsapp_account.credential_validated",
                       "whatsapp_account.health_checked", "whatsapp_account.templates_synced"}
                      <= actions)
                check("32. no secrets exposed in audit trail", "EAAG-smoke-token" not in blob)

            # webhook verification challenge
            r = await c.get("/api/v1/webhooks/whatsapp", params={
                "hub.mode": "subscribe", "hub.verify_token": "smoke-verify-token",
                "hub.challenge": "CH123"})
            check("22. webhook verification works", r.status_code == 200 and r.text == "CH123")
    finally:
        server.should_exit = True
        await serve_task
        await db.close()

    print(f"\nSMOKE RESULT: {PASS} PASS / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
