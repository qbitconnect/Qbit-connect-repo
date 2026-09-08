"""Structured application logging (Brief §18).

- JSON lines in staging/production/test; human-readable console in development.
- Every line carries: timestamp, level, service, request_id (when available),
  user_id (when available), message and optional error/extra fields.
- `redact()` strips secret-like values before anything reaches a sink.
- Never log: passwords, API keys, access tokens, database credentials.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.core import request_context
from app.core.config import Settings

SERVICE_NAME = "qbit-api"

#: keys whose values must never be persisted in logs or audit metadata.
#: Phase 12 (audit M6): extended with common credential-variant key names —
#: exact-match on the lowercased key, checked recursively on every structured
#: log field and audit metadata payload.
SECRET_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "database_url",
    "redis_url",
    "secret_key",
    "vault_key",
    "credentials",
    # --- Phase 12 additions (variant key names seen in provider payloads) ---
    "app_secret",
    "client_secret",
    "verify_token",
    "webhook_secret",
    "shared_secret",
    "smtp_password",
    "email_api_key",
    "maps_provider_api_key",
    "bearer",
    "credential",
    "app_secret_proof",
    "private_key",
    "secret_token",
    "auth_token",
    "session_token",
    "invitation_token",
}


def redact(value: Any) -> Any:
    """Return a copy of `value` with secret-like keys masked. Recursive, safe."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in SECRET_KEYS:
                out[k] = "***REDACTED***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_context.get_request_id()
        if request_id:
            payload["request_id"] = request_id
        user_id = request_context.get_user_id()
        if user_id:
            payload["user_id"] = user_id
        for key in ("request_id", "user_id", "job_id", "action", "status_code", "path", "method"):
            if hasattr(record, key) and getattr(record, key) is not None:
                payload.setdefault(key, getattr(record, key))
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        import json

        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human format for local development only."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        rid = request_context.get_request_id() or "-"
        base = f"{ts} {record.levelname:<8} [{rid[:14]}] {record.name}: {record.getMessage()}"
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            base += f" | {redact(extra)}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(settings: Settings) -> None:
    """Idempotently configure the root logger + uvicorn loggers."""
    root = logging.getLogger()
    if getattr(root, "_qbit_configured", False):
        return

    level = getattr(logging, settings.QBIT_LOG_LEVEL.upper(), logging.INFO)
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    if settings.QBIT_ENV == "development":
        console.setFormatter(ConsoleFormatter())
    else:
        console.setFormatter(JsonFormatter())
    root.addHandler(console)

    try:
        log_dir = settings.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            Path(log_dir) / "qbit-api.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    except OSError:  # pragma: no cover - unwritable disk should not kill the app
        root.warning("Could not open log file handler; logging to stdout only")

    for noisy in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        lg = logging.getLogger(noisy)
        lg.handlers.clear()
        lg.propagate = True

    root._qbit_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_with(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Emit `message` with redacted structured `fields`."""
    logger.log(level, message, extra={"extra_fields": redact(fields)})
