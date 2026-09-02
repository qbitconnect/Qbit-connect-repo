"""Progress reporting — honest, batched, never fake (brief §38).

- Counters (found/saved/duplicates/failed) are accumulated in memory and
  flushed to the DB on an interval (default 3 s) or item threshold — never one
  UPDATE per record (brief §44).
- `progress` percent is set ONLY when a total denominator is known
  (max_results / max_pages estimate). Unknown totals keep progress=0 and the
  UI shows raw record counts instead of a fake percentage.
"""

from __future__ import annotations

import time


class ProgressReporter:
    def __init__(
        self,
        flush,
        *,
        total: int | None = None,
        flush_interval: float = 3.0,
        flush_every_items: int = 200,
        initial: dict | None = None,
    ) -> None:
        self._flush = flush  # async callable(counters: dict) -> None
        self.total = total
        self._interval = flush_interval
        self._every_items = flush_every_items
        # Counters are CUMULATIVE across attempts/resumes: they start from the
        # job row's persisted values so pause/resume never resets totals (§38).
        initial = initial or {}
        self.records_found = int(initial.get("records_found", 0))
        self.records_saved = int(initial.get("records_saved", 0))
        self.records_duplicate = int(initial.get("records_duplicate", 0))
        self.records_failed = int(initial.get("records_failed", 0))
        self.items_processed = 0
        self.pages_fetched = 0
        self.stage: str = "initializing"
        self._last_flush = time.monotonic()
        self._dirty = False

    # ------------------------------------------------------------- counters
    def add_found(self, n: int = 1) -> None:
        self.records_found += n
        self.items_processed += n
        self._dirty = True

    def add_saved(self, n: int = 1) -> None:
        self.records_saved += n
        self._dirty = True

    def add_duplicate(self, n: int = 1) -> None:
        self.records_duplicate += n
        self._dirty = True

    def add_failed(self, n: int = 1) -> None:
        self.records_failed += n
        self._dirty = True

    def add_page(self, n: int = 1) -> None:
        self.pages_fetched += n
        self._dirty = True

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self._dirty = True

    # -------------------------------------------------------------- percent
    @property
    def progress(self) -> float:
        if self.total and self.total > 0:
            return round(min(self.records_found / self.total * 100.0, 100.0), 1)
        return 0.0

    @property
    def counters(self) -> dict:
        return {
            "records_found": self.records_found,
            "records_saved": self.records_saved,
            "records_duplicate": self.records_duplicate,
            "records_failed": self.records_failed,
            "items_processed": self.items_processed,
            "pages_fetched": self.pages_fetched,
            "progress": self.progress,
            "stage": self.stage,
        }

    # ---------------------------------------------------------------- flush
    async def maybe_flush(self, *, force: bool = False) -> None:
        due = (
            force
            or self._dirty
            and (
                time.monotonic() - self._last_flush >= self._interval
                or self.records_found % max(self._every_items, 1) == 0
                and self.records_found > 0
            )
        )
        if not due:
            return
        await self.flush()

    async def flush(self) -> None:
        await self._flush(dict(self.counters))
        self._last_flush = time.monotonic()
        self._dirty = False
