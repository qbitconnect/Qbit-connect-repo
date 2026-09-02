"""Deterministic lead quality score (Phase 4 §15).

This is a DATA COMPLETENESS score, not an AI prediction. Weights:
    +20 business name      +20 phone          +20 email
    +15 website            +10 address        +5 city
    +5 state               +5 source provenance
Normalized to 0-100. Deliberately isolated so a smarter engine can replace
`compute_quality_score` later without touching callers.
"""

from __future__ import annotations

WEIGHTS: tuple[tuple[str, int], ...] = (
    ("business_name", 20),
    ("phone", 20),
    ("email", 20),
    ("website", 15),
    ("address", 10),
    ("city", 5),
    ("state", 5),
)

PROVENANCE_WEIGHT = 5
MAX_SCORE = 100


def _present(value) -> bool:
    return value is not None and str(value).strip() != ""


def compute_quality_score(data: dict) -> int:
    """Score a lead-like dict (display fields). Deterministic and stable."""
    score = 0
    for field, weight in WEIGHTS:
        if _present(data.get(field)):
            score += weight
    if any(_present(data.get(k)) for k in ("source", "source_url", "source_actor_id", "import_batch_id")):
        score += PROVENANCE_WEIGHT
    return min(score, MAX_SCORE)
