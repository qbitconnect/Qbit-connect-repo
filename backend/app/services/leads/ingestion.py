"""LeadIngestionService — the single, centralized scraper→lead path (§26).

    Scrape Result → schema validation → normalization → source provenance
                  → duplicate detection → Lead create/update → activity → stats

Individual actors NEVER contain ingestion logic. The Phase 3 ResultPipeline
delegates its per-item work here; future feeders (webhooks, connectors) reuse
the same service. `commit=False` by default so callers own the transaction.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services.leads.activity import EVENT_SCRAPED, LeadActivityService
from app.services.leads.quality import compute_quality_score
from app.services.leads.service import LeadService
from app.services.scraping.dedup import MatchConfidence, MatchResult

logger = get_logger("qbit.leads.ingestion")


class IngestionResult:
    __slots__ = ("lead", "created", "merged", "flagged", "match")

    def __init__(self, lead, created: bool, merged: bool, flagged: bool, match: MatchResult | None):
        self.lead = lead
        self.created = created
        self.merged = merged
        self.flagged = flagged
        self.match = match


class LeadIngestionService:
    def __init__(
        self,
        *,
        actor_id: str,
        actor_version: str,
        job_id: uuid.UUID,
        source: str | None = None,
        source_type: str = "scraper",
        created_by: uuid.UUID | None = None,
    ) -> None:
        self.actor_id = actor_id
        self.actor_version = actor_version
        self.job_id = job_id
        self.source = source or actor_id
        self.source_type = source_type
        self.created_by = created_by
        self.leads = LeadService()
        self.activities = LeadActivityService()

    async def ingest(
        self,
        session: AsyncSession,
        item: dict,
        *,
        match: MatchResult | None = None,
        merge: bool = False,
        commit: bool = False,
    ) -> IngestionResult:
        """Persist one normalized item with full provenance.

        `item` is the pipeline's normalized dict (contains *_norm keys,
        business_name, metadata, tags, source_url, scraped_at, ...).
        """
        item = dict(item)
        provenance = {
            "source_type": self.source_type,
            "source_id": item.pop("source_id", None) or item.get("metadata", {}).get("source_id"),
        }
        lead, created = await self.leads.create_or_update(
            session,
            item,
            actor_id=self.actor_id,
            actor_version=self.actor_version,
            job_id=self.job_id,
            match=match,
            merge=merge,
            created_by=self.created_by,
            commit=commit,
        )
        # new leads have no PK until flush (python-side uuid default) — the
        # activity/tag writes below reference lead.id, so flush now
        await session.flush()

        # ensure provenance fields survive even on the legacy update path
        changed = False
        if getattr(lead, "source_type", None) in (None, ""):
            lead.source_type = provenance["source_type"]
            changed = True
        if provenance["source_id"] and getattr(lead, "source_id", None) in (None, ""):
            lead.source_id = str(provenance["source_id"])[:300]
            changed = True
        if changed or lead.quality_score is None:
            lead.quality_score = compute_quality_score(lead.to_public_dict())

        # sync relational tags from the item's tag names (mirror stays in sync)
        tag_names = item.get("tags") or []
        if tag_names and created:
            from app.services.leads.tags import TagService

            await TagService().sync_names(session, lead.id, tag_names, user_id=self.created_by)

        if created:
            await self.activities.log(
                session, lead.id, EVENT_SCRAPED,
                message=f"Scraped by {self.actor_id} v{self.actor_version} (job {str(self.job_id)[:8]})",
                metadata={
                    "actor_id": self.actor_id,
                    "actor_version": self.actor_version,
                    "job_id": str(self.job_id),
                    "matched_on": match.matched_on if match else None,
                },
                user_id=self.created_by,
            )
            # Phase 9 §7: LEAD_SCRAPED / LEAD_IMPORTED triggers
            await _emit_automation(
                session,
                event_type=(
                    "lead.imported" if self.source_type == "import" else "lead.scraped"
                ),
                entity_type="lead", entity_id=lead.id,
                payload={
                    "job_id": str(self.job_id), "actor_id": self.actor_id,
                    "source_type": self.source_type,
                },
            )
        elif merge and match is not None and match.confidence is not None:
            await self.activities.log(
                session, lead.id, "lead_updated",
                message=f"Re-seen via {self.actor_id} (matched on {match.matched_on or 'key'})",
                metadata={"job_id": str(self.job_id), "confidence": match.confidence.value},
                user_id=self.created_by,
            )

        # The duplicate flag is written only on the information-preserving
        # MEDIUM-insert path (confidence != NONE, merge=False), so its mere
        # presence identifies a flagged record. (Previously this required
        # `not created`, which is impossible on that path — flagged was
        # always False and the pipeline counter never counted anything.)
        flagged = bool((lead.metadata_json or {}).get("possible_duplicate_of"))
        return IngestionResult(lead, created, merge and not created, flagged, match)


async def _emit_automation(session, *, event_type: str, entity_type: str,
                           entity_id, payload: dict | None = None) -> None:
    """Best-effort automation event intake (Phase 9 §12) — never breaks the
    ingestion flow (same contract as AuditService)."""
    try:
        from app.automation.services.event_dispatcher import emit_system_event

        await emit_system_event(
            session, event_type=event_type, entity_type=entity_type,
            entity_id=entity_id, payload=payload,
        )
    except Exception:  # noqa: BLE001
        pass
