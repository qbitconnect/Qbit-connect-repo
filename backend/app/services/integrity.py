"""Read-only data integrity audit (Phase 12 §48).

Produces DIAGNOSTICS ONLY — never deletes, repairs or "fixes" questionable
records (brief §61). Every check is a pure SELECT (plus existence probes for
file rows); safe to run against a live database.

Checks:
- leads / conversations / campaigns without an organization reference
- messages whose conversation row is missing
- campaigns / scrape jobs / leads whose creator no longer exists
- conversations pointing at a missing assigned team
- duplicate provider event ids (webhook idempotency invariant)
- scrape jobs RUNNING with a stale lease (recovery-sweep candidates)
- file records whose storage object is missing on disk
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import DatabaseManager

logger = get_logger("qbit.integrity")


async def collect_findings() -> dict[str, int]:
    settings = get_settings()
    db = DatabaseManager(settings)
    findings: dict[str, int] = {}
    try:
        async with db.session() as session:
            from app.models.enterprise import Team
            from app.models.file import FileCategory, FileRecord
            from app.models.marketing import Campaign
            from app.models.messaging import Conversation, Message, ProviderEvent
            from app.models.scrape import JobStatus, Lead, ScrapeJob
            from app.models.user import User
            from app.services.audit import AuditService
            from app.services.files import FileService
            from app.services.storage import StorageService

            file_service = FileService(StorageService(settings), AuditService())

            async def count(label: str, stmt) -> None:
                findings[label] = int(await session.scalar(stmt) or 0)

            # --- organization-less rows (should be 0 after migration 0010) ---
            await count(
                "leads_without_organization",
                select(func.count()).select_from(Lead).where(Lead.organization_id.is_(None)),
            )
            await count(
                "conversations_without_organization",
                select(func.count()).select_from(Conversation).where(
                    Conversation.organization_id.is_(None)
                ),
            )
            await count(
                "campaigns_without_organization",
                select(func.count()).select_from(Campaign).where(
                    Campaign.organization_id.is_(None)
                ),
            )

            # --- messages pointing at a missing conversation -----------------
            await count(
                "messages_with_missing_conversation",
                select(func.count())
                .select_from(Message)
                .outerjoin(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.id.is_(None)),
            )

            # --- creators that no longer exist -------------------------------
            for label, model in (
                ("leads_missing_creator", Lead),
                ("campaigns_missing_creator", Campaign),
                ("scrape_jobs_missing_creator", ScrapeJob),
            ):
                creator = getattr(model, "created_by", None)
                if creator is None:
                    findings[label] = 0
                    continue
                await count(
                    label,
                    select(func.count())
                    .select_from(model)
                    .outerjoin(User, creator == User.id)
                    .where(creator.is_not(None) & User.id.is_(None)),
                )

            # --- invalid team references -------------------------------------
            await count(
                "conversations_with_missing_team",
                select(func.count())
                .select_from(Conversation)
                .outerjoin(Team, Conversation.assigned_team_id == Team.id)
                .where(
                    Conversation.assigned_team_id.is_not(None) & Team.id.is_(None)
                ),
            )

            # --- duplicate provider event ids --------------------------------
            await count(
                "duplicate_provider_event_ids",
                select(func.count()).select_from(
                    select(
                        ProviderEvent.provider,
                        ProviderEvent.provider_event_id,
                    )
                    .group_by(ProviderEvent.provider, ProviderEvent.provider_event_id)
                    .having(func.count() > 1)
                    .subquery()
                ),
            )

            # --- RUNNING scrape jobs with a stale lease (report only) --------
            stale_cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=settings.QBIT_WORKER_LEASE_SECONDS * 3
            )
            await count(
                "scrape_jobs_running_with_stale_lease",
                select(func.count()).select_from(ScrapeJob).where(
                    ScrapeJob.status == JobStatus.RUNNING,
                    ScrapeJob.leased_at < stale_cutoff,
                ),
            )

            # --- file records whose object vanished --------------------------
            missing = 0
            checked = 0
            rows = await session.execute(
                select(FileRecord.path, FileRecord.category)
                .where(FileRecord.deleted_at.is_(None))
                .limit(20000)
            )
            for path_value, category_value in rows.all():
                checked += 1
                try:
                    root = file_service.storage.root_for(category_value)
                    if not (root / path_value).exists():
                        missing += 1
                except Exception:  # noqa: BLE001 — probe errors count as findings
                    missing += 1
            findings["file_records_missing_on_disk"] = missing
            findings["file_records_checked"] = checked
            del FileCategory
    finally:
        await db.close()
    return findings


async def run_integrity_audit() -> int:
    """CLI entrypoint: prints a report; exit 0 = clean, 1 = findings."""
    findings = await collect_findings()
    checks = {k: v for k, v in findings.items() if k != "file_records_checked"}
    total = sum(checks.values())
    print({
        "status": "clean" if total == 0 else "findings",
        "total": total,
        "checks": checks,
        "files_probed": findings.get("file_records_checked", 0),
    })
    return 0 if total == 0 else 1


if __name__ == "__main__":  # direct execution convenience
    raise SystemExit(asyncio.run(run_integrity_audit()))
