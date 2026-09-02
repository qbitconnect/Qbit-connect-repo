"""SQLAlchemy models — Phase 2 core + Phase 3 scraping + Phase 4 lead workspace."""

from app.models.audit import AuditLog
from app.models.connection import Connection
from app.models.file import FileRecord
from app.models.lead import (
    ImportBatch,
    LeadActivity,
    LeadDuplicateCandidate,
    LeadExportRecord,
    LeadMergeHistory,
    LeadNote,
    LeadTag,
    LeadTagAssignment,
    SavedView,
)
from app.models.rbac import Permission, Role
from app.models.scrape import Lead, ScrapeJob, ScrapeJobCheckpoint, ScrapeJobEvent
from app.models.setting import SystemSetting
from app.models.user import User

__all__ = [
    "AuditLog",
    "Connection",
    "FileRecord",
    "ImportBatch",
    "Lead",
    "LeadActivity",
    "LeadDuplicateCandidate",
    "LeadExportRecord",
    "LeadMergeHistory",
    "LeadNote",
    "LeadTag",
    "LeadTagAssignment",
    "Permission",
    "Role",
    "SavedView",
    "ScrapeJob",
    "ScrapeJobCheckpoint",
    "ScrapeJobEvent",
    "SystemSetting",
    "User",
]
