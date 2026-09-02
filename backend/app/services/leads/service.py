"""LeadService — the ONLY writer of lead rows (Phase 3 brief §22; Phase 4 §10-§14).

Actors never touch lead tables directly; the result pipeline calls this
service with normalized items. Responsibilities: create, update-on-match,
source tracking, metadata merge.

Phase 4 extensions: server-side search / validated advanced filters /
whitelisted sorting / pagination, manual create+update with normalization,
archive/restore, efficient bulk actions, quality aggregates.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scrape import Lead

#: custom (non-default) statuses must look like canonical workflow codes
_CUSTOM_STATUS_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,29}$")


def _merge_metadata(existing: dict, incoming: dict) -> dict:
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key in ("possible_duplicate_of",):
            continue
        if value in (None, "", {}, []):
            continue
        if key not in merged:
            merged[key] = value
        elif merged[key] != value and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_metadata(merged[key], value)
        elif merged[key] != value:
            history = merged.setdefault(f"{key}_history", [])
            if not isinstance(history, list):
                history = [history]
            if merged[key] not in history:
                history.append(merged[key])
            merged[key] = value
    return merged


def _merge_social(existing: dict, incoming: dict) -> dict:
    merged = dict(existing or {})
    for platform, url in (incoming or {}).items():
        if url and not merged.get(platform):
            merged[platform] = url
    return merged


class LeadService:
    async def create_or_update(
        self,
        session: AsyncSession,
        item: dict,
        *,
        actor_id: str,
        actor_version: str,
        job_id: uuid.UUID,
        match=None,  # MatchResult | None
        merge: bool = False,
        created_by: uuid.UUID | None = None,
        commit: bool = True,
    ) -> tuple[Lead, bool]:
        """Insert a new lead or update the matched one.

        Returns (lead, created). `item` is a normalized lead dict (with
        *_norm keys produced by the normalizer). With `commit=False` the
        caller owns the transaction — the pipeline commits once per BATCH
        instead of once per record (brief §44).
        """
        now = datetime.now(timezone.utc)
        base = dict(
            business_name=item.get("business_name"),
            contact_name=item.get("contact_name"),
            email=item.get("email"),
            phone=item.get("phone"),
            website=item.get("website"),
            address=item.get("address"),
            city=item.get("city"),
            state=item.get("state"),
            country=item.get("country"),
            category=item.get("category"),
            rating=item.get("rating"),
            review_count=item.get("review_count"),
        )

        if merge and match is not None and match.lead_id is not None:
            lead = await session.get(Lead, match.lead_id)
            if lead is not None:
                for key, value in base.items():
                    if value is not None and (getattr(lead, key) is None or getattr(lead, key) == ""):
                        setattr(lead, key, value)
                lead.social_links = _merge_social(lead.social_links or {}, item.get("social_links") or {})
                lead.metadata_json = _merge_metadata(
                    lead.metadata_json or {},
                    {
                        **(item.get("metadata") or {}),
                        "last_job_id": str(job_id),
                        "last_actor_id": actor_id,
                        "last_match_confidence": match.confidence.value if match.confidence else None,
                        "matched_on": match.matched_on,
                    },
                )
                lead.seen_count = (lead.seen_count or 1) + 1
                lead.last_seen_at = now
                if item.get("scraped_at"):
                    lead.scraped_at = item["scraped_at"]
                if commit:
                    await session.commit()
                    await session.refresh(lead)
                return lead, False

        metadata = dict(item.get("metadata") or {})
        if (
            match is not None
            and not merge
            and match.lead_id is not None
            and match.confidence is not None
            and match.confidence.value != "NONE"
        ):
            # information-preserving duplicate flag (brief §21)
            metadata["possible_duplicate_of"] = str(match.lead_id)
            metadata["possible_duplicate_confidence"] = (
                match.confidence.value if match.confidence else None
            )

        lead = Lead(
            **base,
            social_links=item.get("social_links") or {},
            metadata_json=metadata,
            tags=item.get("tags") or [],
            email_norm=item.get("email_norm"),
            phone_norm=item.get("phone_norm"),
            website_norm=item.get("website_norm"),
            name_key=item.get("name_key"),
            source=item.get("source") or actor_id,
            source_url=item.get("source_url"),
            source_actor_id=actor_id,
            source_actor_version=actor_version,
            source_job_id=job_id,
            scraped_at=item.get("scraped_at") or now,
            first_seen_at=now,
            last_seen_at=now,
            created_by=created_by,
        )
        session.add(lead)
        if commit:
            await session.commit()
            await session.refresh(lead)
        return lead, True

    async def count_for_job(self, session: AsyncSession, job_id: uuid.UUID) -> int:
        return int(
            await session.scalar(
                select(func.count()).select_from(Lead).where(Lead.source_job_id == job_id)
            )
            or 0
        )

    async def list_for_job(
        self, session: AsyncSession, job_id: uuid.UUID, *, page: int = 1, page_size: int = 50
    ) -> tuple[list[Lead], int]:
        query = select(Lead).where(Lead.source_job_id == job_id)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(Lead.created_at.asc()).offset((page - 1) * page_size).limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def update_lead(self, session: AsyncSession, lead_id: uuid.UUID, changes: dict) -> Lead | None:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            return None
        for key, value in changes.items():
            if hasattr(lead, key) and key not in ("id", "created_at"):
                setattr(lead, key, value)
        lead.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(lead)
        return lead

    async def archive(self, session: AsyncSession, lead_id: uuid.UUID) -> Lead | None:
        return await self.update_lead(session, lead_id, {"status": "archived"})


# ===========================================================================
#                      Phase 4 — data workspace extensions
# ===========================================================================
from datetime import datetime as _dt, timedelta  # noqa: E402

from sqlalchemy import and_, delete as sa_delete, not_  # noqa: E402

from app.core.errors import NotFoundError as _NotFoundError, ValidationError as _ValidationError  # noqa: E402
from app.core.logging import get_logger as _get_logger  # noqa: E402
from app.models.lead import LeadDuplicateCandidate, LeadNote, LeadStatus  # noqa: E402
from app.services.leads import filters as _filters  # noqa: E402
from app.services.leads.activity import (  # noqa: E402
    EVENT_ARCHIVED,
    EVENT_CREATED,
    EVENT_RESTORED,
    EVENT_STATUS_CHANGED,
    EVENT_TAG_ADDED,
    EVENT_TAG_REMOVED,
    EVENT_UPDATED,
    LeadActivityService,
)
from app.services.leads.normalization import normalize_lead_payload  # noqa: E402
from app.services.leads.quality import compute_quality_score  # noqa: E402
from app.services.leads.tags import TagService  # noqa: E402

_logger = _get_logger("qbit.leads.service")

BULK_DELETE_HARD_LIMIT = 1000


class LeadWorkspaceService:
    """Read/query/edit surface for the data workspace. Writes still flow
    through LeadService (or the shared session/transaction of the caller)."""

    def __init__(self) -> None:
        self.activities = LeadActivityService()
        self.tags = TagService()

    # ------------------------------------------------------------------ read
    @staticmethod
    def search_condition(search: str):
        """Validated multi-column LIKE condition for free-text search
        (wildcards escaped; reusable by the export builder)."""
        if not search:
            return None
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        return or_(
            Lead.business_name.ilike(like, escape="\\"),
            Lead.contact_name.ilike(like, escape="\\"),
            Lead.first_name.ilike(like, escape="\\"),
            Lead.last_name.ilike(like, escape="\\"),
            Lead.phone.ilike(like, escape="\\"),
            Lead.email.ilike(like, escape="\\"),
            Lead.website.ilike(like, escape="\\"),
            Lead.city.ilike(like, escape="\\"),
            Lead.state.ilike(like, escape="\\"),
            Lead.country.ilike(like, escape="\\"),
            Lead.category.ilike(like, escape="\\"),
            Lead.source.ilike(like, escape="\\"),
        )

    async def search(
        self,
        session: AsyncSession,
        *,
        page: int = 1,
        page_size: int = 25,
        search: str | None = None,
        filters: dict | list | None = None,
        sort: str | None = None,
        include_archived: bool = False,
        include_merged: bool = False,
        ids: list[uuid.UUID] | None = None,
    ) -> tuple[list[Lead], int]:
        query = select(Lead)
        if not include_merged:
            query = query.where(Lead.merged_into_id.is_(None))
        if not include_archived:
            query = query.where(Lead.status != LeadStatus.ARCHIVED.value)
        if ids is not None:
            if not ids:
                return [], 0
            query = query.where(Lead.id.in_(ids))
        if search:
            condition = self.search_condition(search)
            if condition is not None:
                query = query.where(condition)
        if filters:
            query = query.where(_filters.build_filter_condition(filters))

        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        order = _filters.build_order_by(sort)
        rows = await session.execute(
            query.order_by(*order).offset((page - 1) * page_size).limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    async def count_all(self, session: AsyncSession) -> int:
        return int(
            await session.scalar(
                select(func.count()).select_from(Lead).where(Lead.merged_into_id.is_(None))
            )
            or 0
        )

    async def get(self, session: AsyncSession, lead_id: uuid.UUID) -> Lead:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            raise _NotFoundError("Lead not found")
        return lead

    # ----------------------------------------------------------------- write
    async def create_lead(
        self,
        session: AsyncSession,
        payload: dict,
        *,
        user_id: uuid.UUID | None = None,
        source: str = "manual",
        source_type: str = "manual",
        tags: list[str] | None = None,
        status: str | None = None,
        commit: bool = True,
    ) -> Lead:
        clean, errors = normalize_lead_payload(payload)
        if errors:
            raise _ValidationError("Validation failed", details={"fields": errors})
        if not clean.get("business_name") and not clean.get("contact_name"):
            raise _ValidationError("business_name or contact_name is required")
        now = datetime.now(timezone.utc)
        lead = Lead(
            **{k: v for k, v in clean.items() if not k.endswith("_norm") and k != "name_key"},
            email_norm=clean.get("email_norm"),
            phone_norm=clean.get("phone_norm"),
            website_norm=clean.get("website_norm"),
            name_key=clean.get("name_key"),
            source=source,
            source_type=source_type,
            status=(status or LeadStatus.NEW.value),
            quality_score=None,  # set below
            first_seen_at=now,
            last_seen_at=now,
            scraped_at=payload.get("scraped_at"),
            created_by=user_id,
        )
        lead.quality_score = compute_quality_score(lead.to_public_dict())  # includes provenance
        session.add(lead)
        await session.flush()
        if tags:
            for name in tags:
                await self.tags.assign(session, lead.id, name, user_id=user_id, commit=False)
            await self.tags._refresh_mirror(session, lead.id)
        await self.activities.log(
            session, lead.id, EVENT_CREATED,
            message="Lead created", user_id=user_id,
        )
        if commit:
            await session.commit()
            await session.refresh(lead)
        return lead

    async def apply_update(
        self,
        session: AsyncSession,
        lead: Lead,
        payload: dict,
        *,
        user_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> Lead:
        """Field-editable update (§19): normalize before store, track changes."""
        clean, errors = normalize_lead_payload(payload)
        if errors:
            raise _ValidationError("Validation failed", details={"fields": errors})
        changed: dict[str, dict] = {}
        for key, value in clean.items():
            if key.endswith("_norm") or key == "name_key":
                continue
            old = getattr(lead, key, None)
            if (old or None) != (value or None):
                changed[key] = {"from": old, "to": value}
            setattr(lead, key, value)
        for key in ("email_norm", "phone_norm", "website_norm", "name_key"):
            if key in clean:
                setattr(lead, key, clean[key])
        if changed:
            lead.quality_score = compute_quality_score(lead.to_public_dict())
            lead.updated_at = datetime.now(timezone.utc)
            await self.activities.log(
                session, lead.id, EVENT_UPDATED,
                message="Lead updated: " + ", ".join(sorted(changed.keys())),
                metadata={"changes": changed},
                user_id=user_id,
            )
        if commit:
            await session.commit()
            await session.refresh(lead)
        return lead

    async def set_status(
        self, session: AsyncSession, lead: Lead, status: str, *,
        user_id: uuid.UUID | None = None, commit: bool = True,
    ) -> Lead:
        allowed = {s.value for s in LeadStatus}
        if status not in allowed and not _CUSTOM_STATUS_RE.match(status or ""):
            raise _ValidationError(f"Unknown status: {status}")
        old = lead.status
        if old == status:
            return lead
        lead.status = status
        lead.updated_at = datetime.now(timezone.utc)
        if status == LeadStatus.ARCHIVED.value:
            lead.archived_at = lead.archived_at or datetime.now(timezone.utc)
            await self.activities.log(session, lead.id, EVENT_ARCHIVED,
                                      message=f"Status {old} → {status}", user_id=user_id)
        elif old == LeadStatus.ARCHIVED.value:
            lead.archived_at = None
            await self.activities.log(session, lead.id, EVENT_RESTORED,
                                      message=f"Restored from {old} → {status}", user_id=user_id)
        else:
            await self.activities.log(session, lead.id, EVENT_STATUS_CHANGED,
                                      message=f"Status {old} → {status}", user_id=user_id)
        if commit:
            await session.commit()
            await session.refresh(lead)
        return lead

    async def archive(self, session: AsyncSession, lead: Lead, *, user_id=None, commit: bool = True) -> Lead:
        return await self.set_status(session, lead, LeadStatus.ARCHIVED.value, user_id=user_id, commit=commit)

    async def restore(self, session: AsyncSession, lead: Lead, *, user_id=None, commit: bool = True) -> Lead:
        return await self.set_status(session, lead, LeadStatus.NEW.value, user_id=user_id, commit=commit)

    # ------------------------------------------------------------------ bulk
    async def bulk_action(
        self,
        session: AsyncSession,
        *,
        action: str,
        lead_ids: list[uuid.UUID],
        user_id: uuid.UUID | None = None,
        params: dict | None = None,
        hard_delete_allowed: bool = False,
    ) -> dict:
        """Efficient bulk operations — set-based SQL, never one query per lead."""
        params = params or {}
        lead_ids = list(dict.fromkeys(lead_ids))
        if not lead_ids:
            raise _ValidationError("No leads selected")
        counts = {"matched": len(lead_ids), "affected": 0}

        if action in ("set_status", "archive", "restore"):
            status = params.get("status") if action == "set_status" else (
                LeadStatus.ARCHIVED.value if action == "archive" else LeadStatus.NEW.value
            )
            allowed = {s.value for s in LeadStatus}
            if status not in allowed and not _CUSTOM_STATUS_RE.match(status or ""):
                raise _ValidationError(f"Unknown status: {status}")
            values = {"status": status, "updated_at": datetime.now(timezone.utc)}
            if status == LeadStatus.ARCHIVED.value:
                values["archived_at"] = datetime.now(timezone.utc)
            if status != LeadStatus.ARCHIVED.value:
                values["archived_at"] = None
            result = await session.execute(
                update(Lead)
                .where(Lead.id.in_(lead_ids), Lead.merged_into_id.is_(None))
                .values(**values)
            )
            counts["affected"] = result.rowcount or 0
            event = EVENT_STATUS_CHANGED
            if action == "archive":
                event = EVENT_ARCHIVED
            elif action == "restore":
                event = EVENT_RESTORED
            await self.activities.log_many(
                session,
                [{"lead_id": lid, "event_type": event,
                  "message": f"Bulk {action} → {status}", "user_id": user_id}
                 for lid in lead_ids[:BULK_DELETE_HARD_LIMIT]],
            )
            await session.commit()

        elif action in ("add_tag", "remove_tag"):
            if action == "add_tag":
                names = params.get("tags") or []
                if not names:
                    raise _ValidationError("tags required")
                counts["affected"] = await self.tags.bulk_assign(
                    session, lead_ids, [str(n) for n in names], user_id=user_id
                )
            else:
                tag_ids = params.get("tag_ids") or []
                if not tag_ids:
                    raise _ValidationError("tag_ids required")
                counts["affected"] = await self.tags.bulk_unassign(
                    session, lead_ids, [u if isinstance(u, uuid.UUID) else uuid.UUID(str(u)) for u in tag_ids]
                )
            for lid in lead_ids[:BULK_DELETE_HARD_LIMIT]:
                await self.activities.log(
                    session, lid,
                    EVENT_TAG_ADDED if action == "add_tag" else EVENT_TAG_REMOVED,
                    message=f"Bulk {action}", user_id=user_id,
                )
            await session.commit()

        elif action == "delete":
            # Prefer soft delete. Hard delete is an explicit, bounded,
            # permission-checked administrative action (§14).
            if params.get("hard") is True:
                if not hard_delete_allowed:
                    raise _ValidationError("Hard delete requires the leads.delete permission")
                if params.get("confirm") != "DELETE":
                    raise _ValidationError('Hard delete requires confirm="DELETE"')
                if len(lead_ids) > BULK_DELETE_HARD_LIMIT:
                    raise _ValidationError(
                        f"Hard delete is capped at {BULK_DELETE_HARD_LIMIT} leads per call"
                    )
                result = await session.execute(
                    sa_delete(Lead).where(Lead.id.in_(lead_ids))
                )
                counts["affected"] = result.rowcount or 0
                await session.commit()
                _logger.warning(
                    "Leads hard-deleted",
                    extra={"extra_fields": {"count": counts["affected"], "by": str(user_id)}},
                )
            else:
                result = await session.execute(
                    update(Lead)
                    .where(Lead.id.in_(lead_ids), Lead.merged_into_id.is_(None))
                    .values(
                        status=LeadStatus.ARCHIVED.value,
                        archived_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                counts["affected"] = result.rowcount or 0
                await self.activities.log_many(
                    session,
                    [{"lead_id": lid, "event_type": EVENT_ARCHIVED,
                      "message": "Bulk delete (soft → ARCHIVED)", "user_id": user_id}
                     for lid in lead_ids[:BULK_DELETE_HARD_LIMIT]],
                )
                await session.commit()
        else:
            raise _ValidationError(f"Unknown bulk action: {action}")
        return counts

    # ------------------------------------------------------------------ notes
    async def add_note(
        self, session: AsyncSession, lead_id: uuid.UUID, content: str, *,
        user_id: uuid.UUID | None = None, commit: bool = True,
    ) -> LeadNote:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            raise _NotFoundError("Lead not found")
        text = str(content or "").strip()
        if not text:
            raise _ValidationError("Note content must not be empty")
        if len(text) > 20000:
            raise _ValidationError("Note too long (max 20000 chars)")
        note = LeadNote(lead_id=lead_id, user_id=user_id, content=text)
        session.add(note)
        await self.activities.log(
            session, lead_id, "note_added", message="Note added", user_id=user_id
        )
        if commit:
            await session.commit()
            await session.refresh(note)
        return note

    async def list_notes(
        self, session: AsyncSession, lead_id: uuid.UUID, *, page: int = 1, page_size: int = 50
    ) -> tuple[list[LeadNote], int]:
        query = select(LeadNote).where(LeadNote.lead_id == lead_id)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await session.execute(
            query.order_by(LeadNote.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.scalars().all()), int(total or 0)

    # --------------------------------------------------------------- quality
    async def quality_stats(self, session: AsyncSession) -> dict:
        """All numbers from real queries — the UI never invents statistics."""
        base = select(Lead).where(Lead.merged_into_id.is_(None))

        async def _count(extra=None) -> int:
            query = base
            if extra is not None:
                query = query.where(extra)
            return int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)

        non_empty = lambda col: (col.isnot(None)) & (col != "")  # noqa: E731

        total = await _count()
        complete = await _count(
            and_(
                non_empty(Lead.business_name), non_empty(Lead.phone), non_empty(Lead.email),
                non_empty(Lead.website), non_empty(Lead.address), non_empty(Lead.city),
            )
        )
        return {
            "total": total,
            "complete": complete,
            "missing_phone": await _count(not_(non_empty(Lead.phone))),
            "missing_email": await _count(not_(non_empty(Lead.email))),
            "missing_website": await _count(not_(non_empty(Lead.website))),
            "missing_address": await _count(not_(non_empty(Lead.address))),
            "low_quality": await _count(Lead.quality_score < 40),
            "duplicates_pending": int(
                await session.scalar(
                    select(func.count())
                    .select_from(LeadDuplicateCandidate)
                    .where(LeadDuplicateCandidate.status == "PENDING")
                )
                or 0
            ),
            "recently_imported": await _count(Lead.source_type == "import"),
            "recently_scraped": await _count(Lead.source_type == "scraper"),
        }

    async def recompute_quality(
        self, session: AsyncSession, *, batch: int = 1000, commit: bool = True
    ) -> int:
        """Backfill/recompute quality scores (deterministic formula)."""
        updated = 0
        offset = 0
        while True:
            rows = (
                await session.scalars(
                    select(Lead)
                    .where(Lead.merged_into_id.is_(None), Lead.quality_score.is_(None))
                    .order_by(Lead.created_at)
                    .offset(offset)
                    .limit(batch)
                )
            ).all()
            if not rows:
                break
            for lead in rows:
                lead.quality_score = compute_quality_score(lead.to_public_dict())
                updated += 1
            offset += batch
        if commit:
            await session.commit()
        return updated


def _utc_days_ago(days: int) -> _dt:
    return datetime.now(timezone.utc) - timedelta(days=days)
