"""QBIT Connect analytics & reporting engine (Phase 10).

Read-only analytics over operational data:
- core/         time periods, allowlisted filters, cache, exceptions, math
- domains/      one module per analytics domain (leads, scraping, marketing,
                whatsapp, email, inbox, automation, team, overview)
- aggregation.py  daily aggregate refresh/rebuild (derived, idempotent)
- reports/      saved reports, background runs, snapshots, exports

Rules (spec §3): real data only; deterministic; permission-aware; analytics
never modifies operational data — the only rows it writes are its own
report/aggregate metadata.
"""

from __future__ import annotations
