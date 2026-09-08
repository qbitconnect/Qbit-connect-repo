"""Team management endpoints (Phase 11 §6, §33).

All checks flow through AuthorizationService — routes never hand-roll policy.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import DbSession, get_client_ip, require_context
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.models.enterprise import Team, TeamMember
from app.models.user import User
from app.schemas.common import PageMeta
from app.services.authorization import MemberContext
from app.schemas.enterprise import (
    TeamActionOut,
    TeamCreate,
    TeamListOut,
    TeamMemberIn,
    TeamMembersOut,
    TeamOut,
    TeamUpdate,
)

router = APIRouter(prefix="/teams", tags=["teams"])

CtxDep = require_context("teams.view")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "team"


def _to_out(team: Team, member_count: int) -> TeamOut:
    return TeamOut(
        id=str(team.id),
        organization_id=str(team.organization_id),
        name=team.name,
        slug=team.slug,
        description=team.description,
        is_active=team.is_active,
        member_count=member_count,
        created_at=team.created_at,
    )


async def _count_members(session, team_id) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team_id)
        )
        or 0
    )


@router.get("", response_model=TeamListOut)
async def list_teams(
    session: DbSession,
    ctx: MemberContext = Depends(CtxDep),
    include_inactive: bool = Query(default=False),
):
    query = select(Team).where(Team.organization_id == ctx.organization_id)
    if not include_inactive:
        query = query.where(Team.is_active.is_(True))
    query = query.order_by(Team.name.asc())
    rows = (await session.execute(query)).scalars().all()
    counts = {
        team_id: int(c)
        for team_id, c in (
            await session.execute(
                select(TeamMember.team_id, func.count())
                .where(TeamMember.team_id.in_([t.id for t in rows]))
                .group_by(TeamMember.team_id)
            )
        ).all()
    } if rows else {}
    return TeamListOut(
        data=[_to_out(t, counts.get(t.id, 0)) for t in rows],
        meta=PageMeta(page=1, page_size=len(rows), total=len(rows)),
    )


@router.post("", response_model=TeamActionOut, status_code=201)
async def create_team(
    payload: TeamCreate,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("teams.create")),
):
    slug = _slugify(payload.name)
    duplicate = await session.scalar(
        select(Team).where(
            Team.organization_id == ctx.organization_id, Team.slug == slug, Team.is_active.is_(True)
        )
    )
    if duplicate is not None:
        raise ConflictError("A team with this name already exists")
    team = Team(
        organization_id=ctx.organization_id,
        name=payload.name.strip(),
        slug=slug,
        description=payload.description,
        created_by=ctx.user.id,
    )
    session.add(team)
    await session.commit()
    await session.refresh(team)
    return TeamActionOut(data=_to_out(team, 0))


async def _get_org_team(session, team_id: uuid.UUID, ctx: MemberContext) -> Team:
    team = await session.get(Team, team_id)
    if team is None or team.organization_id != ctx.organization_id:
        raise NotFoundError("Team not found")
    return team


@router.get("/{team_id}", response_model=TeamActionOut)
async def get_team(
    team_id: uuid.UUID,
    session: DbSession,
    ctx: MemberContext = Depends(CtxDep),
):
    team = await _get_org_team(session, team_id, ctx)
    return TeamActionOut(data=_to_out(team, await _count_members(session, team.id)))


@router.patch("/{team_id}", response_model=TeamActionOut)
async def update_team(
    team_id: uuid.UUID,
    payload: TeamUpdate,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("teams.edit")),
):
    team = await _get_org_team(session, team_id, ctx)
    changed = {}
    if payload.name is not None:
        team.name = payload.name.strip()
        team.slug = _slugify(team.name)
        changed["name"] = team.name
    if payload.description is not None:
        team.description = payload.description
        changed["description"] = payload.description
    if payload.is_active is not None:
        team.is_active = payload.is_active
        changed["is_active"] = payload.is_active
    await session.commit()
    await session.refresh(team)
    return TeamActionOut(
        data=_to_out(team, await _count_members(session, team.id))
    )


@router.get("/{team_id}/members", response_model=TeamMembersOut)
async def list_members(
    team_id: uuid.UUID,
    session: DbSession,
    ctx: MemberContext = Depends(CtxDep),
):
    team = await _get_org_team(session, team_id, ctx)
    rows = (
        await session.execute(
            select(TeamMember, User)
            .join(User, User.id == TeamMember.user_id)
            .where(TeamMember.team_id == team.id)
            .order_by(TeamMember.created_at.asc())
        )
    ).all()
    return TeamMembersOut(
        data=[
            TeamMemberOut(
                user_id=str(tm.user_id),
                email=u.email,
                full_name=u.full_name,
                is_lead=tm.is_lead,
                joined_at=tm.created_at,
            )
            for tm, u in rows
        ]
    )


@router.post("/{team_id}/members", response_model=TeamMembersOut, status_code=201)
async def add_member(
    team_id: uuid.UUID,
    payload: TeamMemberIn,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("teams.manage_members")),
):
    team = await _get_org_team(session, team_id, ctx)
    try:
        user_id = uuid.UUID(payload.user_id)
    except ValueError as exc:
        raise NotFoundError("User not found") from exc
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    # the added user must belong to the same organization (no cross-tenant leaks)
    from app.services.authorization import get_membership

    membership = await get_membership(session, user_id, ctx.organization_id)
    if membership is None:
        raise PermissionDeniedError("User is not a member of this organization")

    existing = await session.scalar(
        select(TeamMember).where(
            TeamMember.team_id == team.id, TeamMember.user_id == user_id
        )
    )
    if existing is not None:
        existing.is_lead = payload.is_lead  # idempotent re-add
    else:
        session.add(TeamMember(team_id=team.id, user_id=user_id, is_lead=payload.is_lead))
    await session.commit()
    return await list_members(team_id, session, ctx)


@router.delete("/{team_id}/members/{user_id}", response_model=TeamMembersOut)
async def remove_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    session: DbSession,
    ctx: MemberContext = Depends(require_context("teams.manage_members")),
):
    team = await _get_org_team(session, team_id, ctx)
    row = await session.scalar(
        select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == user_id)
    )
    if row is not None:
        await session.delete(row)
        await session.commit()
    return await list_members(team_id, session, ctx)
