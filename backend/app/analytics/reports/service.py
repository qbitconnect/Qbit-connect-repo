"""Report service — CRUD, ownership/visibility enforcement, run lifecycle.

Visibility model (spec §18, §26, §32):
- PRIVATE: owner + reports.manage holders see the report
- TEAM: any signed-in reports.view holder (no team table exists yet — same
  posture as the Phase 8 workspace)
- GLOBAL: everyone with reports.view
- mutating actions require ownership OR reports.manage; run/export require
  reports.run / reports.export AND visibility
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.core.exceptions import ReportConfigError
from app.analytics.core.filters import filters_from_payload
from app.analytics.core.time import resolve_period
from app.analytics.reports.schemas import validate_report_config, validate_report_name
from app.core.errors import NotFoundError, PermissionDeniedError
from app.models.analytics import Report, ReportRun, ReportRunStatus, ReportSnapshot, ReportStatus, ReportVisibility
from app.models.user import User


def can_view(report: Report, user: User, permissions: set[str]) -> bool:
    if "reports.manage" in permissions:
        return True
    if report.visibility == ReportVisibility.GLOBAL:
        return True
    if report.visibility == ReportVisibility.TEAM:
        return True
    return report.owner_id is not None and report.owner_id == user.id


def can_manage(report: Report, user: User, permissions: set[str]) -> bool:
    if "reports.manage" in permissions:
        return True
    return report.owner_id is not None and report.owner_id == user.id


class ReportService:
    async def list(
        self, session: AsyncSession, *, user: User, permissions: set[str],
        page: int = 1, page_size: int = 25, include_archived: bool = False,
    ) -> tuple[list[Report], int]:
        base = select(Report)
        if "reports.manage" not in permissions:
            base = base.where(
                (Report.visibility != ReportVisibility.PRIVATE)
                | (Report.owner_id == user.id)
            )
        if not include_archived:
            base = base.where(Report.status == ReportStatus.ACTIVE)
        total = int(await session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0)
        rows = (await session.execute(
            base.order_by(Report.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
        return list(rows), total

    async def get_visible(
        self, session: AsyncSession, report_id: uuid.UUID,
        *, user: User, permissions: set[str],
    ) -> Report:
        report = await session.get(Report, report_id)
        if report is None:
            raise NotFoundError("Report not found")
        if not can_view(report, user, permissions):
            # do not leak existence across owners (IDOR posture, spec §32)
            raise NotFoundError("Report not found")
        return report

    async def get_manageable(
        self, session: AsyncSession, report_id: uuid.UUID,
        *, user: User, permissions: set[str],
    ) -> Report:
        report = await self.get_visible(session, report_id, user=user, permissions=permissions)
        if not can_manage(report, user, permissions):
            raise PermissionDeniedError("Only the report owner or a manager can modify it")
        return report

    async def create(
        self, session: AsyncSession, *, name: str, description: str | None,
        config: dict, visibility: str, owner: User, timezone: str = "UTC",
    ) -> Report:
        validated = validate_report_config(config)
        visibility = (visibility or "PRIVATE").upper()
        if visibility not in (ReportVisibility.PRIVATE, ReportVisibility.TEAM,
                              ReportVisibility.GLOBAL):
            raise ReportConfigError("visibility must be PRIVATE, TEAM or GLOBAL")
        report = Report(
            name=validate_report_name(name),
            description=(description or "").strip() or None,
            config=validated,
            domain=validated["domain"],
            visibility=visibility,
            owner_id=owner.id,
            created_by=owner.id,
            timezone=timezone or "UTC",
        )
        session.add(report)
        await session.commit()
        return report

    async def update(
        self, session: AsyncSession, report: Report, *,
        name: str | None = None, description: str | None = None,
        config: dict | None = None, visibility: str | None = None,
        timezone: str | None = None,
    ) -> Report:
        if name is not None:
            report.name = validate_report_name(name)
        if description is not None:
            report.description = description.strip() or None
        if config is not None:
            report.config = validate_report_config(config)
            report.domain = report.config["domain"]
            report.config_version += 1
        if visibility is not None:
            visibility = visibility.upper()
            if visibility not in (ReportVisibility.PRIVATE, ReportVisibility.TEAM,
                                  ReportVisibility.GLOBAL):
                raise ReportConfigError("visibility must be PRIVATE, TEAM or GLOBAL")
            report.visibility = visibility
        if timezone is not None:
            # validate via resolve_tz; an invalid zone is rejected, never guessed
            resolve_period(period="today", tz_name=timezone)
            report.timezone = timezone
        await session.commit()
        return report

    async def archive(self, session: AsyncSession, report: Report) -> Report:
        from datetime import datetime, timezone as dt_timezone

        report.status = ReportStatus.ARCHIVED
        report.archived_at = datetime.now(dt_timezone.utc)
        await session.commit()
        return report

    async def restore(self, session: AsyncSession, report: Report) -> Report:
        report.status = ReportStatus.ACTIVE
        report.archived_at = None
        await session.commit()
        return report

    async def duplicate(
        self, session: AsyncSession, report: Report, *, user: User,
    ) -> Report:
        copy = Report(
            name=f"{report.name} (copy)"[:200],
            description=report.description,
            domain=report.domain,
            config=dict(report.config or {}),
            config_version=report.config_version,
            visibility=ReportVisibility.PRIVATE,  # copies always start private
            owner_id=user.id,
            created_by=user.id,
            timezone=report.timezone,
        )
        session.add(copy)
        await session.commit()
        return copy

    async def delete(self, session: AsyncSession, report: Report) -> None:
        await session.delete(report)
        await session.commit()

    # ------------------------------------------------------------------ runs
    async def queue_run(
        self, session: AsyncSession, report: Report, *, user: User,
        format_name: str = "json",
    ) -> ReportRun:
        if report.status != ReportStatus.ACTIVE:
            raise ReportConfigError("Archived reports cannot be executed (restore first)")
        run = ReportRun(
            report_id=report.id,
            status=ReportRunStatus.QUEUED,
            requested_by=user.id,
            config_snapshot=dict(report.config or {}),
            config_version=report.config_version,
            timezone=report.timezone,
            format=format_name,
        )
        session.add(run)
        await session.commit()
        return run

    async def list_runs(
        self, session: AsyncSession, report: Report, *, page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ReportRun], int]:
        base = select(ReportRun).where(ReportRun.report_id == report.id)
        total = int(await session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0)
        rows = (await session.execute(
            base.order_by(ReportRun.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
        return list(rows), total

    async def latest_snapshot(
        self, session: AsyncSession, report: Report,
    ) -> ReportSnapshot | None:
        return (await session.execute(
            select(ReportSnapshot)
            .where(ReportSnapshot.report_id == report.id)
            .order_by(ReportSnapshot.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()

    async def snapshot_for_run(
        self, session: AsyncSession, report: Report, run_id: uuid.UUID,
    ) -> ReportSnapshot | None:
        run = await session.get(ReportRun, run_id)
        if run is None or run.report_id != report.id:
            raise NotFoundError("Run not found")
        return (await session.execute(
            select(ReportSnapshot).where(ReportSnapshot.run_id == run_id)
        )).scalar_one_or_none()

    # ------------------------------------------------------- execution input
    def run_request(self, run: ReportRun):
        """Build the analytics request for a stored run (reproducible: the
        frozen config + stored timezone are used, never the report's current
        config — spec §19 'historical reports must remain reproducible')."""
        config = dict(run.config_snapshot or {})
        period_key = str(config.get("period", "30d"))
        date_from = config.get("date_from")
        date_to = config.get("date_to")
        return period_key, date_from, date_to
