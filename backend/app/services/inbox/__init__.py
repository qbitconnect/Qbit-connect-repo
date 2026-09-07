"""Unified inbox services (Phase 8).

    normalizer.py  provider payloads → UnifiedInboundMessage (channel-neutral)
    engine.py      ConversationEngine — ingestion, threading, lead matching,
                   workflow (status/priority/assignment), delivery statuses
    workspace.py   list/search/counters/read-state/notes/activity queries
    reply.py       outbound replies — validation, idempotency, outbox enqueue
    outbox.py      worker-side delivery through the campaign provider registry

The inbox is provider-independent (§1): WhatsApp and Email are ingested
through the same normalized shape and replies leave through the SAME provider
abstraction campaigns use — never a second send stack.
"""

from app.services.inbox.engine import ConversationEngine
from app.services.inbox.normalizer import UnifiedInboundMessage
from app.services.inbox.outbox import OutboxService
from app.services.inbox.reply import ReplyService
from app.services.inbox.workspace import InboxWorkspace

__all__ = [
    "ConversationEngine",
    "InboxWorkspace",
    "OutboxService",
    "ReplyService",
    "UnifiedInboundMessage",
]
