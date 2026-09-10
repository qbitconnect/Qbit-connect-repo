"""Dataset-level change detection (Actor Platform spec §7.B 'AD CHANGE DETECTION').

Every run is a snapshot dataset. Comparing a current snapshot against a
previous one — keyed by a stable field (default `change_key`, which the
meta-ads actor sets to `meta-ad:<ad_id>`) — classifies each key:

  new        present now, absent before
  unchanged  present in both, same content_hash
  modified   present in both, different content_hash
  stopped    present before, absent now
  resumed    present now, absent before, but present in an optional BASELINE
             snapshot (three-way; only when the caller supplies one)

The comparison uses the actors' own `content_hash` field when present
(meta-ads) and falls back to a canonical JSON hash of the record otherwise.
Purely derived from real stored data (spec §42).
"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.actor_platform import ActorDatasetItem

STATUS_NEW = "new"
STATUS_UNCHANGED = "unchanged"
STATUS_MODIFIED = "modified"
STATUS_STOPPED = "stopped"
STATUS_RESUMED = "resumed"


def record_fingerprint(data: dict) -> str:
    explicit = data.get("content_hash")
    if explicit:
        return str(explicit)
    material = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


async def _load_index(session: AsyncSession, dataset_id: uuid.UUID, key_field: str) -> dict[str, dict]:
    rows = (
        await session.execute(
            select(ActorDatasetItem)
            .where(ActorDatasetItem.dataset_id == dataset_id)
            .order_by(ActorDatasetItem.idx)
        )
    ).scalars().all()
    index: dict[str, dict] = {}
    for row in rows:
        data = row.data or {}
        key = data.get(key_field)
        if key:
            index[str(key)] = data
    return index


async def compare_snapshots(
    session: AsyncSession,
    previous_dataset_id: uuid.UUID,
    current_dataset_id: uuid.UUID,
    *,
    key_field: str = "change_key",
    baseline_dataset_id: uuid.UUID | None = None,
) -> list[dict]:
    """Returns one row per key with status + fingerprint evidence.

    `baseline_dataset_id` enables the three-way `resumed` classification:
    keys that are new-vs-previous but existed in the baseline snapshot."""
    before = await _load_index(session, previous_dataset_id, key_field)
    now = await _load_index(session, current_dataset_id, key_field)
    baseline = (
        await _load_index(session, baseline_dataset_id, key_field)
        if baseline_dataset_id is not None
        else {}
    )

    changes: list[dict] = []
    for key, current in now.items():
        prev = before.get(key)
        current_fp = record_fingerprint(current)
        if prev is None:
            status = (
                STATUS_RESUMED
                if baseline_dataset_id is not None and key in baseline
                else STATUS_NEW
            )
            changes.append({
                "key": key, "status": status,
                "content_hash": current_fp,
            })
        elif record_fingerprint(prev) == current_fp:
            changes.append({
                "key": key, "status": STATUS_UNCHANGED,
                "content_hash": current_fp,
            })
        else:
            changes.append({
                "key": key, "status": STATUS_MODIFIED,
                "content_hash": current_fp,
                "previous_content_hash": record_fingerprint(prev),
            })
    for key, prev in before.items():
        if key not in now:
            changes.append({
                "key": key, "status": STATUS_STOPPED,
                "previous_content_hash": record_fingerprint(prev),
            })
    changes.sort(key=lambda c: (c["status"], c["key"]))
    return changes
