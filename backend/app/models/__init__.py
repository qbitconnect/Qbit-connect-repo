"""SQLAlchemy models — Phase 2 core + Phase 3 scraping + Phase 4 leads + Phase 5 marketing + Phase 6 messaging + Phase 7 email."""

from app.models.audit import AuditLog
from app.models.connection import Connection
from app.models.email import EmailTrackingEvent, EmailUnsubscribeToken
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
from app.models.marketing import (
    Campaign,
    CampaignEvent,
    CampaignQueueItem,
    CampaignRecipient,
    CampaignTemplate,
    OptOutRecord,
    SendingAccount,
    SuppressionEntry,
)
from app.models.messaging import (
    Conversation,
    Message,
    ProviderCredentials,
    ProviderEvent,
)
from app.models.rbac import Permission, Role
from app.models.scrape import Lead, ScrapeJob, ScrapeJobCheckpoint, ScrapeJobEvent
from app.models.setting import SystemSetting
from app.models.user import User

__all__ = [
    "AuditLog",
    "Campaign",
    "CampaignEvent",
    "CampaignQueueItem",
    "CampaignRecipient",
    "CampaignTemplate",
    "Connection",
    "Conversation",
    "EmailTrackingEvent",
    "EmailUnsubscribeToken",
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
    "Message",
    "OptOutRecord",
    "Permission",
    "ProviderCredentials",
    "ProviderEvent",
    "Role",
    "SavedView",
    "ScrapeJob",
    "ScrapeJobCheckpoint",
    "ScrapeJobEvent",
    "SendingAccount",
    "SuppressionEntry",
    "SystemSetting",
    "User",
]
