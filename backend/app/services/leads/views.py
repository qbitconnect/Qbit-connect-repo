"""Saved views (Phase 4 §12): named, validated filter sets.

Visibility: PRIVATE (owner only), TEAM (any signed-in operator), GLOBAL.
RBAC: reading requires leads.view; creating/modifying requires
leads.manage_views — plus ownership for PRIVATE views.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.models.lead import SavedView
from app.services.leads import filters as filter_engine


class SavedViewService:
    async def create(
        self,
        session: AsyncSession,
        *,
        name: str,
        filters: dict | list,
        visibility: str = "PRIVATE",
        owner_id: uuid.UUID | None = None,
        entity: str = "leads",
    ) -> SavedView:
        clean_name = " ".join(str(name or "").split())[:150]
        if not clean_name:
            raise ValidationError("View name must not be empty")
        # validate the filter structure eagerly — never save a broken view
        filter_engine.build_filter_condition(filters)
        visibility = (visibility or "PRIVATE").upper()
        if visibility not in ("PRIVATE", "TEAM", "GLOBAL"):
            raise ValidationError("visibility must be PRIVATE, TEAM or GLOBAL")
        if visibility == "PRIVATE" and owner_id is None:
            raise ValidationError("PRIVATE views require an owner")
        view = SavedView(
            name=clean_name, entity=entity, filters=filters,
            visibility=visibility, owner_id=owner_id,
        )
        session.add(view)
        await session.commit()
        await session.refresh(view)
        return view

    async def list_visible(
        self, session: AsyncSession, *, user_id: uuid.UUID | None, entity: str = "leads",
        page: int = 1, page_size: int = 50,
    ) -> tuple[list[SavedView], int]:
        query = select(SavedView).where(SavedView.entity == entity)
        if user_id is None:
            query = query.where(SavedView.visibility != "PRIVATE")
        else:
            query = query.where(
                or_(
                    SavedView.visibility != "PRIVATE",
                    SavedView.owner_id == user_id,
                )
            )
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(SavedView.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def get_visible(
        self, session: AsyncSession, view_id: uuid.UUID, *, user_id: uuid.UUID | None
    ) -> SavedView:
        view = await session.get(SavedView, view_id)
        if view is None:
            raise NotFoundError("Saved view not found")
        if view.visibility == "PRIVATE" and (user_id is None or view.owner_id != user_id):
            raise NotFoundError("Saved view not found")  # do not leak existence
        return view

    async def rename(
        self, session: AsyncSession, view_id: uuid.UUID, *, name: str,
        user_id: uuid.UUID | None, can_manage_all: bool,
    ) -> SavedView:
        view = await self._owned(session, view_id, user_id=user_id, can_manage_all=can_manage_all)
        clean = " ".join(str(name or "").split())[:150]
        if not clean:
            raise ValidationError("View name must not be empty")
        view.name = clean
        view.updated_at = view.updated_at
        await session.commit()
        await session.refresh(view)
        return view

    async def update_filters(
        self, session: AsyncSession, view_id: uuid.UUID, *, filters: dict | list,
        user_id: uuid.UUID | None, can_manage_all: bool,
    ) -> SavedView:
        view = await self._owned(session, view_id, user_id=user_id, can_manage_all=can_manage_all)
        filter_engine.build_filter_condition(filters)
        view.filters = filters
        view.updated_at = view.updated_at
        await session.commit()
        await session.refresh(view)
        return view

    async def duplicate(
        self, session: AsyncSession, view_id: uuid.UUID, *,
        user_id: uuid.UUID | None, can_manage_all: bool,
    ) -> SavedView:
        source = await self.get_visible(session, view_id, user_id=user_id)
        copy = SavedView(
            name=f"{source.name} (copy)"[:150],
            entity=source.entity,
            filters=source.filters,
            visibility="PRIVATE" if source.visibility == "PRIVATE" else source.visibility,
            owner_id=user_id,
        )
        session.add(copy)
        await session.commit()
        await session.refresh(copy)
        return copy

    async def delete(
        self, session: AsyncSession, view_id: uuid.UUID, *,
        user_id: uuid.UUID | None, can_manage_all: bool,
    ) -> None:
        view = await self._owned(session, view_id, user_id=user_id, can_manage_all=can_manage_all)
        await session.delete(view)
        await session.commit()

    async def _owned(
        self, session: AsyncSession, view_id: uuid.UUID, *,
        user_id: uuid.UUID | None, can_manage_all: bool,
    ) -> SavedView:
        view = await session.get(SavedView, view_id)
        if view is None:
            raise NotFoundError("Saved view not found")
        if not can_manage_all and (view.owner_id is None or view.owner_id != user_id):
            raise PermissionDeniedError("You do not own this saved view")
        return view
