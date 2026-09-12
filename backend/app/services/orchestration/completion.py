"""QBIT CONNECT — Deterministic Completion Engine (Brief §11, §12, §14, §36).

CRITICAL GOVERNANCE RULE:
The Agent / LLM has ZERO authority to declare completion.
Completion is governed strictly by deterministic mathematical and database
state evaluation. Partial results are NEVER faked into full success.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


class CompletionStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    IN_PROGRESS = "IN_PROGRESS"
    VALIDATING = "VALIDATING"
    DEDUPLICATING = "DEDUPLICATING"
    TARGET_REACHED = "TARGET_REACHED"
    SOURCE_EXHAUSTED = "SOURCE_EXHAUSTED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class ProgressSnapshot:
    target: int
    collected: int
    unique: int
    duplicates: int
    invalid: int
    valid_phone: int
    valid_email: int
    queries_completed: int
    queries_total: int
    pages_fetched: int
    status: CompletionStatus
    remaining: int
    completion_percentage: float
    message: str = ""
    is_terminal: bool = False
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


class CompletionEngine:
    """Evaluates task execution progress and strictly computes completion states."""

    @staticmethod
    def evaluate(
        *,
        target: int,
        collected: int,
        unique: int,
        duplicates: int,
        invalid: int,
        valid_phone: int = 0,
        valid_email: int = 0,
        queries_completed: int = 1,
        queries_total: int = 1,
        pages_fetched: int = 0,
        job_status: str = "RUNNING",
        has_fatal_error: bool = False,
        sources_exhausted: bool = False,
        limit_reached: bool = False,
        stop_requested: bool = False,
    ) -> ProgressSnapshot:
        """Determines the real completion status based solely on persistent factual numbers."""
        target = max(1, target)
        remaining = max(0, target - unique)
        pct = min(100.0, round((unique / target) * 100.0, 2))

        status: CompletionStatus
        is_terminal = False
        message: str

        if stop_requested or job_status == "CANCELLED":
            status = CompletionStatus.CANCELLED
            is_terminal = True
            message = f"Execution cancelled by operator with {unique} unique records saved."

        elif has_fatal_error or job_status == "FAILED":
            status = CompletionStatus.FAILED
            is_terminal = True
            message = (
                f"Execution failed after collecting {collected} records ({unique} unique). "
                f"Unrecoverable error encountered."
            )

        elif unique >= target:
            status = CompletionStatus.TARGET_REACHED
            is_terminal = True
            pct = 100.0
            remaining = 0
            message = f"Target reached: {unique} valid unique records extracted (Target: {target})."

        elif sources_exhausted or (queries_completed >= queries_total and job_status in ("COMPLETED", "PAUSED")):
            status = CompletionStatus.SOURCE_EXHAUSTED
            is_terminal = True
            message = (
                f"Source exhausted: {unique} valid unique records extracted across all "
                f"{queries_completed}/{queries_total} query paths. Target of {target} was not reached."
            )

        elif limit_reached or job_status == "COMPLETED":
            if unique >= target:
                status = CompletionStatus.TARGET_REACHED
                is_terminal = True
                pct = 100.0
                remaining = 0
                message = f"Target reached: {unique} records saved."
            else:
                status = CompletionStatus.PARTIAL
                is_terminal = True
                message = (
                    f"Execution completed partial results ({unique}/{target}) "
                    f"due to configured page or resource budget."
                )

        else:
            status = CompletionStatus.IN_PROGRESS
            is_terminal = False
            message = f"In progress: {unique}/{target} unique records saved ({pct}% complete)."

        return ProgressSnapshot(
            target=target,
            collected=collected,
            unique=unique,
            duplicates=duplicates,
            invalid=invalid,
            valid_phone=valid_phone,
            valid_email=valid_email,
            queries_completed=queries_completed,
            queries_total=queries_total,
            pages_fetched=pages_fetched,
            status=status,
            remaining=remaining,
            completion_percentage=pct,
            message=message,
            is_terminal=is_terminal,
        )
