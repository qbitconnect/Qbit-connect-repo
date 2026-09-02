"""Streaming JSONL result writers (brief §24, §45).

Raw / normalized / error records stream to
    QBIT_DATA_DIR/scraper-results/{actor_id}/{job_id}/<kind>.jsonl

Records are appended and flushed in bounded batches — a million-record job
never holds its dataset in RAM. Files live under the SCRAPER_RESULT storage
category so they inherit StorageService path safety.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger("qbit.scrapers.files")

FLUSH_EVERY = 200


class JsonlWriter:
    """Append-only JSONL writer with bounded buffering."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._buffer: io.StringIO = io.StringIO()
        self._pending = 0
        self.count = 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict) -> None:
        self._buffer.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        self._pending += 1
        self.count += 1
        if self._pending >= FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        if self._pending == 0:
            return
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(self._buffer.getvalue())
        self._buffer = io.StringIO()
        self._pending = 0

    def close(self) -> None:
        self.flush()


class JobResultFiles:
    """Owns the three result streams of one job."""

    KINDS = ("raw", "normalized", "errors")

    def __init__(self, storage_root: Path, actor_id: str, job_id: str) -> None:
        base = storage_root / actor_id / job_id
        base.mkdir(parents=True, exist_ok=True)
        self.base = base
        self._writers: dict[str, JsonlWriter] = {}

    def _writer(self, kind: str) -> JsonlWriter:
        if kind not in self._writers:
            self._writers[kind] = JsonlWriter(self.base / f"{kind}.jsonl")
        return self._writers[kind]

    def write_raw(self, record: dict) -> None:
        self._writer("raw").write(record)

    def write_normalized(self, record: dict) -> None:
        self._writer("normalized").write(record)

    def write_error(self, record: dict) -> None:
        self._writer("errors").write(record)

    def counts(self) -> dict[str, int]:
        return {kind: w.count for kind, w in self._writers.items()}

    def close(self) -> None:
        for writer in self._writers.values():
            writer.close()

    def relative_files(self) -> dict[str, str]:
        """Category-relative storage keys (SCRAPER_RESULT root)."""
        return {kind: f"{self.base.parent.name}/{self.base.name}/{kind}.jsonl" for kind in self.KINDS}
