"""Recipient event state machine (Phase 6 §21).

Safe, forward-only transitions for CampaignRecipient delivery states:

    PENDING → QUEUED → SENDING → SENT → DELIVERED → READ → REPLIED
                      ↘ FAILED (from QUEUED/SENDING/SENT only)

Rules:
- a status event only ever moves the recipient FORWARD (READ → QUEUED can
  never happen, no matter what order events arrive in)
- FAILED is a hard terminal state once entered
- out-of-order provider events (delivered arriving before sent) are absorbed
  gracefully — the status jumps forward, timestamps fill in when missing
- the machine never invents data: unknown events are ignored (return False)
"""

from __future__ import annotations

from app.models.marketing import CampaignRecipient, EventType, RecipientStatus

#: forward order (§21) — index order defines "forward"
ORDER: tuple[str, ...] = (
    RecipientStatus.PENDING.value,
    RecipientStatus.QUEUED.value,
    RecipientStatus.SENDING.value,
    RecipientStatus.SENT.value,
    RecipientStatus.DELIVERED.value,
    RecipientStatus.READ.value,
    RecipientStatus.REPLIED.value,
)
_TERMINAL_FORWARD = ORDER[-1]

#: event type → (target status, recipient timestamp attribute)
EVENT_TARGETS: dict[str, tuple[str, str]] = {
    EventType.MESSAGE_SENT: (RecipientStatus.SENT.value, "sent_at"),
    EventType.MESSAGE_DELIVERED: (RecipientStatus.DELIVERED.value, "delivered_at"),
    EventType.MESSAGE_READ: (RecipientStatus.READ.value, "read_at"),
    EventType.MESSAGE_REPLIED: (RecipientStatus.REPLIED.value, "replied_at"),
}

#: statuses from which a FAILED outcome is accepted (§21: SENT → FAILED ok)
FAILED_ALLOWED_FROM: set[str] = {
    RecipientStatus.PENDING.value,
    RecipientStatus.QUEUED.value,
    RecipientStatus.SENDING.value,
    RecipientStatus.SENT.value,
}


def can_transition(current: str, target: str) -> bool:
    """True when current → target is a legal forward (or same) move."""
    if current == RecipientStatus.FAILED.value:
        return False
    if target == RecipientStatus.FAILED.value:
        return current in FAILED_ALLOWED_FROM
    if current not in ORDER or target not in ORDER:
        return False
    return ORDER.index(target) >= ORDER.index(current)


def apply_event(recipient: CampaignRecipient, event_type: str, *, timestamp=None) -> bool:
    """Apply a normalized MESSAGE_* event to a recipient.

    Returns True when the recipient row changed (status moved or a timestamp
    was filled in), False when the event was unapplicable (unknown type,
    backward transition, terminal state) — the caller then decides whether to
    store the raw event anyway (it always does; the recipient just doesn't
    move backwards).
    """
    now_value = None  # timestamps are set by the caller when needed
    event_type = (event_type or "").upper()

    if event_type == EventType.MESSAGE_FAILED:
        if can_transition(recipient.status, RecipientStatus.FAILED.value):
            recipient.status = RecipientStatus.FAILED.value
            if getattr(recipient, "failed_at", None) is None:
                recipient.failed_at = timestamp
            return True
        return False

    target = EVENT_TARGETS.get(event_type)
    if target is None:
        return False
    target_status, timestamp_field = target
    if not can_transition(recipient.status, target_status):
        return False

    changed = False
    if recipient.status != target_status:
        recipient.status = target_status
        changed = True
    if getattr(recipient, timestamp_field, None) is None:
        setattr(recipient, timestamp_field, timestamp)
        changed = True
    return changed
