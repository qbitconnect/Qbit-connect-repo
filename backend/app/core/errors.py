"""Centralized API error handling (Brief §20).

Uniform error envelope — no stack traces in production, no internal paths:

    {
        "success": false,
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "Resource not found",
            "request_id": "req_..."
        }
    }
"""

from __future__ import annotations

import logging

from app.core import request_context
from app.core.logging import get_logger, log_with

logger = get_logger("qbit.errors")


class QBITError(Exception):
    """Base class for all intentional, user-visible API errors."""

    status_code = 400
    code = "BAD_REQUEST"
    message = "Bad request"

    def __init__(self, message: str | None = None, *, details: dict | None = None) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message
        self.details = details or {}


class ValidationError(QBITError):
    status_code = 422
    code = "VALIDATION_ERROR"
    message = "Validation failed"


class UnauthorizedError(QBITError):
    status_code = 401
    code = "UNAUTHORIZED"
    message = "Authentication required"


class AuthFailedError(QBITError):
    status_code = 401
    code = "INVALID_CREDENTIALS"
    message = "Invalid email or password"


class PermissionDeniedError(QBITError):
    status_code = 403
    code = "PERMISSION_DENIED"
    message = "You do not have permission to perform this action"


class NotFoundError(QBITError):
    status_code = 404
    code = "RESOURCE_NOT_FOUND"
    message = "Resource not found"


class ConflictError(QBITError):
    status_code = 409
    code = "CONFLICT"
    message = "Resource already exists"


class PathAccessDeniedError(QBITError):
    """Raised when a storage key attempts path traversal or outside access."""

    status_code = 403
    code = "ACCESS_DENIED"
    message = "Access to the requested path is not allowed"


class StorageError(QBITError):
    status_code = 500
    code = "STORAGE_ERROR"
    message = "Storage operation failed"


class RateLimitedError(QBITError):
    status_code = 429
    code = "TOO_MANY_REQUESTS"
    message = "Too many requests. Please slow down."


class MaintenanceError(QBITError):
    status_code = 503
    code = "MAINTENANCE_MODE"
    message = "System is in maintenance mode"


def error_envelope(code: str, message: str, details: dict | None = None) -> dict:
    err: dict = {
        "code": code,
        "message": message,
        "request_id": request_context.get_request_id(),
    }
    if details:
        err["details"] = details
    return {"success": False, "error": err}


def register_error_handlers(app) -> None:  # noqa: ANN001 - FastAPI app
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(QBITError)
    async def qbit_error_handler(request, exc: QBITError):
        log_with(
            logger,
            logging.INFO if exc.status_code < 500 else logging.ERROR,
            "API error",
            code=exc.code,
            status_code=exc.status_code,
            path=request.url.path,
            method=request.method,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(exc.code, exc.message, exc.details or None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_, exc: RequestValidationError):
        details = {
            ".".join(str(loc) for loc in err.get("loc", []) if loc not in ("body", "query", "path")): err.get("msg")
            for err in exc.errors()
        }
        return JSONResponse(
            status_code=422,
            content=error_envelope("VALIDATION_ERROR", "Validation failed", details or None),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_, exc: StarletteHTTPException):
        code_map = {
            401: "UNAUTHORIZED",
            403: "PERMISSION_DENIED",
            404: "RESOURCE_NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            413: "PAYLOAD_TOO_LARGE",
            429: "TOO_MANY_REQUESTS",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(
                code_map.get(exc.status_code, "HTTP_ERROR"),
                str(exc.detail),
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_, exc: Exception):
        # Log full traceback internally; never expose it to the client.
        log_with(logger, logging.CRITICAL, "Unhandled exception", error=repr(exc))
        logger.critical("Unhandled exception traceback", exc_info=True)
        return JSONResponse(
            status_code=500,
            content=error_envelope("INTERNAL_ERROR", "An internal error occurred"),
        )
