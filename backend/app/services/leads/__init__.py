"""Lead services package (Phase 4).

Import-compatible with the Phase 2/3 module layout:
    from app.services.leads import LeadService
"""

from app.services.leads.activity import LeadActivityService
from app.services.leads.dedup_engine import DuplicateDetectionService, DuplicateMatch
from app.services.leads.ingestion import LeadIngestionService, IngestionResult
from app.services.leads.merge import MergeService
from app.services.leads.normalization import normalize_lead_payload
from app.services.leads.quality import compute_quality_score
from app.services.leads.service import LeadService, LeadWorkspaceService
from app.services.leads.tags import TagService
from app.services.leads.views import SavedViewService

__all__ = [
    "DuplicateDetectionService",
    "DuplicateMatch",
    "IngestionResult",
    "LeadActivityService",
    "LeadIngestionService",
    "LeadService",
    "LeadWorkspaceService",
    "MergeService",
    "SavedViewService",
    "TagService",
    "compute_quality_score",
    "normalize_lead_payload",
]
