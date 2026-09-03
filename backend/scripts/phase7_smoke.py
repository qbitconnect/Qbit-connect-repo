"""Phase 7 end-to-end smoke: real app + isolated SQLite + TEST-ONLY provider.

Proves the full email pipeline: account → validate → template → campaign →
launch → worker-style delivery → signed webhooks → analytics → unsubscribe →
suppression. The mock provider is TEST ONLY; production refuses it.

Run:  python scripts/phase7_smoke.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import os

os.chdir(BACKEND)

import httpx  # noqa: E402

WEBHOOK_SECRET = "smoke-webhook-secret"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


async def main() -> int:
    from sqlalchemy import select

    from alembic import command
    from alembic.config import Config

    tmp = Path(tempfile.mkdtemp(prefix="qbit-phase7-"))
    db_path = tmp / "smoke.db"
    # hostile ambient DATABASE_URL must not leak into alembic env.py
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    # alembic env.py uses asyncio.run — execute it outside this running loop
    await asyncio.to_thread(command.upgrade, cfg, "head")
    check("migrations_upgrade_head", True)

    from app.core.config import Settings
    from app.core.security import hash_password
    from app.db.session import DatabaseManager
    from app.main import create_app
    from app.models.marketing import CampaignRecipient
    from app.models.rbac import Role
    from app.models.scrape import Lead
    from app.models.user import User
    from app.services import rbac as rbac_service

    settings = Settings(
        QBIT_ENV="test",
        QBIT_SECRET_KEY="smoke-secret-key-" + "b" * 47,
        DATABASE_URL=f"sqlite+aiosqlite:///{db_path}",
        QBIT_DATA_DIR=tmp / "data",
        QBIT_EXPORT_DIR=tmp / "data" / "exports",
        QBIT_LOG_DIR=tmp / "logs",
        QBIT_BACKUP_DIR=tmp / "backups",
        QBIT_PUBLIC_BASE_URL="http://smoke",
        QBIT_MARKETING_WEBHOOK_SECRET=WEBHOOK_SECRET,
        QBIT_MARKETING_EMAILS_PER_MINUTE=600,
        _env_file=None,
    )
    db = DatabaseManager(settings)
    app = create_app(settings, db=db)

    async with db.session() as session:
        counts = await rbac_service.seed_rbac(session)
        check("rbac_seed", counts["permissions"] == 58, f"counts={counts}")
        super_role = (
            await session.scalars(select(Role).where(Role.code == "SUPER_ADMIN"))
        ).first()
        admin = User(
            email="admin@smoke.example.com",
            password_hash=hash_password("Sup3rSmoke!Pass1"),
            full_name="Smoke Admin",
        )
        admin.roles.append(super_role)  # transient object — no lazy IO
        session.add(admin)
        session.add(Lead(
            business_name="Smoke Co", first_name="Sagar", last_name="Patel",
            email="sagar@smokeco.test", email_norm="sagar@smokeco.test",
        ))
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://smoke") as client:
        r = await client.post("/api/v1/auth/login", json={
            "email": "admin@smoke.example.com", "password": "Sup3rSmoke!Pass1"})
        token = r.json()["access_token"]
        H = {"Authorization": f"Bearer {token}"}
        check("auth_login", r.status_code == 200)

        # --- sending account (TEST-ONLY mock) --------------------------------
        r = await client.post("/api/v1/connections/email", headers=H, json={
            "name": "Smoke Mailer", "provider": "mock_email",
            "sender_name": "Smoke", "sender_email": "sales@smokeco.test",
            "reply_to": "replies@smokeco.test",
            "config": {"mock_ready": True}, "credentials": {"mode": "success"}})
        account = r.json()["data"]["account"]
        check("account_created_pending", r.status_code == 201 and account["status"] == "PENDING")
        account_blob = json.dumps(account)
        check("credentials_masked",
              '"mode"' not in account_blob and "success" not in account_blob
              and "mock_ready" in account_blob)  # non-secret config IS returned

        r = await client.post(f"/api/v1/connections/email/{account['id']}/validate", headers=H)
        v = r.json()["data"]
        check("account_validated_active",
              v["validation"]["ok"] and v["account"]["status"] == "ACTIVE")

        r = await client.post(f"/api/v1/connections/email/{account['id']}/health", headers=H)
        check("health_check_ok", r.json()["data"]["health"]["healthy"] is True)

        r2 = await client.post("/api/v1/connections/email", headers=H, json={
            "name": "Smoke Support", "provider": "smtp",
            "sender_email": "support@smokeco.test",
            "config": {"host": "127.0.0.1", "port": 2599, "security": "STARTTLS"},
            "credentials": {"username": "u", "password": "p"}})
        check("second_account_created", r2.status_code == 201)

        # --- templates --------------------------------------------------------
        r = await client.post("/api/v1/templates", headers=H, json={
            "name": "Smoke Outreach", "channel": "EMAIL",
            "subject": "Hello {{first_name}}",
            "html_body": "<p>Hi {{first_name}} at {{business_name}}.</p>"
                         "<p><a href=\"https://smokeco.test/offer\">Offer</a></p>"
                         "<p><a href=\"{{unsubscribe_url}}\">Unsubscribe</a></p>",
            "text_body": "Hi {{first_name}} at {{business_name}}."})
        template = r.json()["data"]["template"]
        check("template_created", r.status_code == 201 and "first_name" in template["variables"])

        r = await client.post("/api/v1/templates", headers=H, json={
            "name": "Evil", "channel": "EMAIL", "subject": "{{ system_prompt }}",
            "text_body": "x"})
        check("unknown_variable_rejected", r.status_code == 422)

        r = await client.post("/api/v1/templates", headers=H, json={
            "name": "XSS Probe", "channel": "EMAIL", "subject": "t",
            "html_body": "<p onclick='x()'>ok</p><script>alert(1)</script>"})
        check("template_saved", r.status_code == 201)

        # --- campaign launch ----------------------------------------------------
        r = await client.post("/api/v1/campaigns", headers=H, json={
            "name": "Smoke Campaign", "channel": "EMAIL",
            "template_id": template["id"], "sending_account_id": account["id"],
            "audience": {"require_opt_in": False,
                         "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}},
            "track_opens": True, "track_clicks": True})
        campaign = r.json()["data"]["campaign"]
        check("campaign_created", r.status_code == 201)

        r = await client.post(f"/api/v1/campaigns/{campaign['id']}/launch", headers=H)
        launch = r.json()["data"]["launch"]
        check("campaign_launched_queued", launch["queued"]["queued"] == 1, r.text[:200])

        # --- worker-style delivery ------------------------------------------------
        from app.services.marketing.delivery import EmailDeliveryService
        from app.services.marketing.queue import InProcessMarketingQueue
        delivery = EmailDeliveryService(settings)
        queue = InProcessMarketingQueue()

        async def deliver(campaign_id: str) -> tuple[str, str | None]:
            async with db.session() as session:
                rid = (await session.scalars(
                    select(CampaignRecipient.id).where(
                        CampaignRecipient.campaign_id == uuid.UUID(campaign_id))
                )).first()
                async with db.session() as inner:
                    status = await delivery.process(inner, uuid.UUID(str(rid)), queue=queue)
                recipient = await session.get(CampaignRecipient, uuid.UUID(str(rid)))
                return status, recipient.provider_message_id

        status, provider_message_id = await deliver(campaign["id"])
        check("email_sent", status == "SENT", f"status={status}")
        check("provider_message_id_stored", bool(provider_message_id))

        # --- signed webhooks -----------------------------------------------------
        def signed_body(event: dict) -> tuple[bytes, dict]:
            body = json.dumps({"events": [event]}).encode()
            sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
            return body, {"X-QBIT-Signature": sig, "X-QBIT-Timestamp": str(int(time.time()))}

        body, headers = signed_body({
            "id": "evt-delivered-1", "type": "delivered", "message_id": provider_message_id})
        r = await client.post("/api/v1/webhooks/email/mock_email", content=body, headers=headers)
        check("webhook_delivered_applied",
              r.status_code == 200 and r.json()["data"]["applied"] == 1)

        body, headers = signed_body({
            "id": "evt-delivered-1", "type": "delivered", "message_id": provider_message_id})
        r = await client.post("/api/v1/webhooks/email/mock_email", content=body, headers=headers)
        check("duplicate_webhook_ignored", r.json()["data"]["duplicates"] == 1)

        body, _ = signed_body({"id": "evt-x", "type": "delivered", "message_id": provider_message_id})
        r = await client.post("/api/v1/webhooks/email/mock_email", content=body, headers={
            "X-QBIT-Signature": "sha256=deadbeef", "X-QBIT-Timestamp": str(int(time.time()))})
        check("forged_webhook_rejected", r.status_code == 401)

        # --- tracking --------------------------------------------------------------
        from app.services.marketing import tracking as T

        open_token = T.make_open_token(uuid.UUID(campaign["id"]), uuid.UUID(str(
            (await _first_recipient(db, campaign["id"]))
        )), settings.QBIT_SECRET_KEY)
        r = await client.get(f"/t/open/{open_token}")
        check("open_pixel_recorded", r.status_code == 200
              and r.headers["content-type"].startswith("image/png"))

        click_token = T.make_click_token(uuid.UUID(campaign["id"]), uuid.UUID(str(
            (await _first_recipient(db, campaign["id"]))
        )), "https://smokeco.test/destination", settings.QBIT_SECRET_KEY)
        r = await client.get(f"/t/click/{click_token}", follow_redirects=False)
        check("click_redirect_safe", r.status_code == 302
              and r.headers["location"] == "https://smokeco.test/destination")

        # --- analytics for campaign 1 (open+click, still delivered) ---------------
        r = await client.get(f"/api/v1/campaigns/{campaign['id']}/email/analytics", headers=H)
        analytics = r.json()["data"]["analytics"]
        check("analytics_accurate",
              analytics["verified"]["sent_total"] == 1
              and analytics["verified"]["delivered_total"] == 1
              and analytics["counters"]["opened"] == 1
              and analytics["counters"]["clicked"] == 1,
              json.dumps(analytics["verified"]))

        # --- campaign 2: deliver, then hard bounce → suppression -------------------
        r = await client.post("/api/v1/campaigns", headers=H, json={
            "name": "Smoke 2", "channel": "EMAIL",
            "template_id": template["id"], "sending_account_id": account["id"],
            "audience": {"require_opt_in": False,
                         "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}}})
        c2 = r.json()["data"]["campaign"]
        r = await client.post(f"/api/v1/campaigns/{c2['id']}/launch", headers=H)
        check("second_campaign_launched", r.status_code == 200)
        status2, pmid2 = await deliver(c2["id"])
        check("second_send_sent", status2 == "SENT")

        body, headers = signed_body({
            "id": "evt-bounce-2", "type": "bounce", "hard": True, "message_id": pmid2})
        r = await client.post("/api/v1/webhooks/email/mock_email", content=body, headers=headers)
        check("hard_bounce_suppressed", r.status_code == 200 and r.json()["data"]["applied"] == 1)

        from app.services.marketing.suppression import UnsubscribeService

        unsub = UnsubscribeService(ttl_days=30, base_url="http://smoke")
        rid2 = await _first_recipient(db, c2["id"])
        async with db.session() as session:
            raw = await unsub.issue_token(
                session, channel="EMAIL", address="sagar@smokeco.test",
                address_norm="sagar@smokeco.test", lead_id=None,
                campaign_id=uuid.UUID(c2["id"]), recipient_id=uuid.UUID(str(rid2)))
            await session.commit()
        page = await client.get(f"/unsubscribe/{raw}")
        check("unsubscribe_page_public", page.status_code == 200 and "Confirm unsubscribe" in page.text)
        confirm = await client.post(f"/unsubscribe/{raw}")
        check("unsubscribe_confirmed", "unsubscribed" in confirm.text.lower())

        # --- future campaign to unsubscribed address is skipped -----------------------
        r = await client.post("/api/v1/campaigns", headers=H, json={
            "name": "Smoke 3", "channel": "EMAIL",
            "template_id": template["id"], "sending_account_id": account["id"],
            "audience": {"require_opt_in": False,
                         "filter": {"and": [{"field": "has_email", "op": "eq", "value": True}]}}})
        c3 = r.json()["data"]["campaign"]
        r = await client.post(f"/api/v1/campaigns/{c3['id']}/launch", headers=H)
        check("third_campaign_launched", r.status_code == 200)
        async with db.session() as session:
            rid3 = (await session.scalars(
                select(CampaignRecipient).where(
                    CampaignRecipient.campaign_id == uuid.UUID(c3["id"]))
            )).first()
            # the address carries a HARD_BOUNCE suppression (upgrade-protected)
            # and the unsubscribe recorded OPTED_OUT consent — either way the
            # send is honestly SKIPPED, never queued
            check("unsubscribed_never_sent",
                  rid3.status == "SKIPPED" and rid3.reason in ("UNSUBSCRIBED", "SUPPRESSED"),
                  f"{rid3.status}/{rid3.reason}")

        # --- suppression API -----------------------------------------------------------
        r = await client.get("/api/v1/suppressions?channel=EMAIL", headers=H)
        reasons = {s["reason"] for s in r.json()["data"]["items"]}
        check("suppression_api_lists", "HARD_BOUNCE" in reasons, str(reasons))
        from sqlalchemy import select as _sel

        from app.models.marketing import MarketingConsent

        async with db.session() as session:
            consent = (await session.scalars(
                _sel(MarketingConsent).where(
                    MarketingConsent.address_norm == "sagar@smokeco.test")
            )).first()
            check("unsubscribe_consent_recorded",
                  consent is not None and consent.opt_in_status == "OPTED_OUT")

        # --- campaign list --------------------------------------------------------------
        r = await client.get("/api/v1/campaigns", headers=H)
        check("campaign_list", r.json()["data"]["total"] == 3)

    await db.close()
    await app.state.redis.close()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{'=' * 60}\nPHASE 7 SMOKE: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    for name, ok, detail in failed:
        print(f"  FAILED: {name} {detail}")
    return 1 if failed else 0


async def _first_recipient(db, campaign_id: str) -> str:
    from sqlalchemy import select

    from app.models.marketing import CampaignRecipient

    async with db.session() as session:
        rid = (await session.scalars(
            select(CampaignRecipient.id).where(
                CampaignRecipient.campaign_id == uuid.UUID(campaign_id))
        )).first()
        return str(rid)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
