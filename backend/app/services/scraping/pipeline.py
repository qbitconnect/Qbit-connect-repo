"""Result pipeline (brief §20):

    raw item → validation/normalization → dedup → lead upsert → counters
             → JSONL writers (raw + normalized + errors)

- Invalid records are written to errors.jsonl and counted — never saved as
  leads (brief §20).
- Batching: leads are accumulated and upserted per batch (default 100) with
  batched match lookups (brief §44).
- Every item also streams to disk (JSONL) so large jobs stay memory-flat.
"""

from __future__ import annotations

import uuid
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.scrapers.core.exceptions import ScraperCancelledError, ScraperPausedError
from app.scrapers.core.netguard import canonical_url
from app.services.leads import LeadIngestionService
from app.services.scraping.dedup import Deduplicator, MatchConfidence
from app.services.scraping.normalizer import normalize_item
from app.services.scraping.result_files import JobResultFiles

logger = get_logger("qbit.scrapers.pipeline")


class ResultPipeline:
    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_id: str,
        actor_version: str,
        job_id: uuid.UUID,
        source: str | None = None,
        result_files: JobResultFiles,
        dedup: Deduplicator | None = None,
        batch_size: int = 100,
        created_by: uuid.UUID | None = None,
        ctx=None,  # ScraperContext for stop checks + counters (optional in tests)
    ) -> None:
        self.session = session
        self.actor_id = actor_id
        self.actor_version = actor_version
        self.job_id = job_id
        self.source = source or actor_id
        self.files = result_files
        self.dedup = dedup or Deduplicator()
        self.batch_size = batch_size
        self.created_by = created_by
        self.ctx = ctx
        self.ingestion = LeadIngestionService(
            actor_id=actor_id,
            actor_version=actor_version,
            job_id=job_id,
            source=self.source,
            created_by=created_by,
        )
        self._batch: list[dict] = []
        self.saved_ids: list[uuid.UUID] = []

    # ------------------------------------------------------------------ feed
    async def add(self, raw_item: dict, *, raw_record: dict | None = None) -> dict:
        """Process one raw item through the pipeline. Returns a per-item
        accounting dict {outcome: saved|duplicate|invalid|flagged}."""
        self.files.write_raw(raw_record if raw_record is not None else raw_item)
        if self.ctx is not None:
            self.ctx.progress.add_found()

        normalized = normalize_item(raw_item, source=self.source)
        if normalized is None:
            self.files.write_error(
                {"reason": "normalization_failed", "item": _safe_truncate(raw_item)}
            )
            if self.ctx is not None:
                self.ctx.progress.add_failed()
            return {"outcome": "invalid"}

        normalized["source_url"] = (
            canonical_url(normalized["source_url"]) if normalized.get("source_url") else None
        )
        self.files.write_normalized(normalized)
        self._batch.append(normalized)
        if len(self._batch) >= self.batch_size:
            await self.process_batch()
        return {"outcome": "queued"}

    async def add_batch(self, items: list[dict]) -> list[dict]:
        return [await self.add(item) for item in items]

    # ----------------------------------------------------------------- batch
    async def process_batch(self, *, stop_checks: bool = True) -> dict:
        """Dedup + upsert the accumulated batch. Called automatically when the
        batch fills and manually at end-of-stream / checkpoint boundaries.

        ONE transaction per batch (brief §44): LeadService runs with
        commit=False and this method commits once at the end.
        `stop_checks=False` is used by the runner's finalize path — the
        stop decision was already made and flushing must complete.
        """
        batch, self._batch = self._batch, []
        if not batch:
            return {"saved": 0, "duplicate": 0, "flagged": 0}
        saved = duplicate = flagged = 0
        try:
            for item in batch:
                if stop_checks and self.ctx is not None:
                    await self.ctx.check_stopped()  # cooperative stop between items
                match = await self.dedup.find_match(self.session, item)
                merge = self.dedup.should_merge(match)
                # Phase 4: the per-item path is centralized in LeadIngestionService
                # (provenance + quality + tags + activity) — pipeline keeps batching,
                # file streaming and the single transaction per batch.
                result = await self.ingestion.ingest(
                    self.session, item, match=match, merge=merge, commit=False
                )
                lead, created = result.lead, result.created
                if result.flagged:
                    # information-preserving MEDIUM insert: new lead carrying a
                    # possible-duplicate flag (implies created — branch FIRST)
                    flagged += 1
                    saved += 1
                    if self.ctx is not None:
                        self.ctx.progress.add_saved()
                    # Source graph (spec §QBIT DIFFERENTIATION #1/#2): store the
                    # cross-record SAME_BUSINESS evidence for later resolution.
                    if match.matched_on and match.matched_on != "fuzzy_name":
                        await self._record_entity_link(match, lead)
                elif merge:
                    duplicate += 1
                    if self.ctx is not None:
                        self.ctx.progress.add_duplicate()
                        # Phase 4 §26: this item UPDATED an existing lead
                        self.ctx.progress.add_updated()
                        await self.ctx.report("ITEM_UPDATED", str(lead.id), {"matched_on": match.matched_on})
                elif created:
                    saved += 1
                    if self.ctx is not None:
                        self.ctx.progress.add_saved()
                        await self.ctx.report("ITEM_SAVED", str(lead.id))
                    if len(self.saved_ids) < 100000:
                        self.saved_ids.append(lead.id)
                self.dedup.note_decision(match, merged=(not created))
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return {"saved": saved, "duplicate": duplicate, "flagged": flagged}

    # ------------------------------------------------------------------- end
    async def _record_entity_link(self, match, new_lead) -> None:
        """Best-effort source-graph evidence row; must never break the batch."""
        try:
            from app.models.scrape import EntityLink

            existing_org = getattr(new_lead, "organization_id", None)
            existing_lead = await self.session.get(type(new_lead), match.lead_id)
            link = EntityLink(
                lead_id=new_lead.id,
                related_lead_id=match.lead_id,
                relation="SAME_BUSINESS",
                matched_by=match.matched_on or "name_key",
                confidence=match.confidence.value if match.confidence else "MEDIUM",
                status="ACTIVE",
                # same convention as the leads themselves: the ACTOR THAT RAN
                source_actor_a=getattr(new_lead, "source_actor_id", None),
                source_actor_b=(getattr(existing_lead, "source_actor_id", None) if existing_lead else None),
                detected_by_job_id=(self.ctx.job_id if self.ctx is not None else None),
                organization_id=existing_org,
            )
            self.session.add(link)
        except Exception:  # noqa: BLE001 — evidence is advisory
            logger.warning("EntityLink recording skipped", exc_info=True)

    async def finish(self) -> dict:
        await self.process_batch()
        self.files.close()
        return {
            "saved": self.ctx.progress.records_saved if self.ctx else None,
            "duplicate": self.ctx.progress.records_duplicate if self.ctx else None,
            "files": self.files.counts(),
        }


def _safe_truncate(item: Any, limit: int = 4000) -> Any:
    try:
        text = repr(item)
        return text[:limit]
    except Exception:  # noqa: BLE001
        return None
