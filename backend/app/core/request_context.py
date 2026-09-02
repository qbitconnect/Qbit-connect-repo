"""Per-request correlation context (Brief §19).

A `request_id` is generated per request (or accepted from an inbound
`X-Request-ID` header), stored in a ContextVar, and surfaced in:
- every structured log line (logging.py)
- API error envelopes (errors.py)
- the `X-Request-ID` response header
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def get_request_id() -> str | None:
    return _request_id.get()


def set_user_id(user_id: str | None) -> None:
    _user_id.set(user_id)


def get_user_id() -> str | None:
    return _user_id.get()
