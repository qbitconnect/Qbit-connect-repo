"""QBIT Connect API — FastAPI application factory (Brief §21, §25, §19, §20)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.core import request_context
from app.core.config import CATEGORY_DIRS, Settings
from app.core.errors import MaintenanceError, register_error_handlers
from app.core.logging import get_logger, log_with, setup_logging
from app.core.ratelimit import SlidingWindowRateLimiter
from app.db.session import DatabaseManager
from app.redis_client import RedisManager
from app.services.audit import AuditService
from app.services.files import FileService
from app.services.health import HealthService
from app.services.storage import StorageService

logger = get_logger("qbit.app")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Generates/propagates X-Request-ID; binds it to logs (Brief §19)."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        request_id = request.headers.get("x-request-id") or request_context.new_request_id()
        request_context.set_request_id(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            pass
        response.headers["X-Request-ID"] = request_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers (Brief §25 / architecture doc 17)."""

    HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        for key, value in self.HEADERS.items():
            response.headers.setdefault(key, value)
        return response


class MaintenanceMiddleware(BaseHTTPMiddleware):
    """Blocks mutating traffic when system maintenance mode is enabled."""

    READ_METHODS = {"GET", "HEAD", "OPTIONS"}

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.method not in self.READ_METHODS:
            settings: Settings = request.app.state.settings
            if settings.QBIT_ENV != "test" and getattr(request.app.state, "maintenance_mode", False):
                from app.core.errors import error_envelope

                return JSONResponse(status_code=503, content=error_envelope("MAINTENANCE_MODE", MaintenanceError.message))
        try:
            return await call_next(request)
        except MaintenanceError:
            from app.core.errors import error_envelope

            return JSONResponse(status_code=503, content=error_envelope("MAINTENANCE_MODE", MaintenanceError.message))


def ensure_storage_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    for sub in CATEGORY_DIRS.values():
        (settings.data_dir / sub).mkdir(parents=True, exist_ok=True)
    for extra in ("attachments", "cache", "temporary", "database"):
        (settings.data_dir / extra).mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)


def create_app(settings: Settings | None = None, *, db: DatabaseManager | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings)

    problems = settings.validate_runtime()
    if problems:
        for p in problems:
            logger.critical("Configuration problem: %s", p)
        raise RuntimeError("; ".join(problems))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_storage_dirs(settings)
        # Initial actor health snapshot (non-fatal; /scrapers/health refreshes)
        try:
            await app.state.scraper_registry.health_check()
        except Exception:  # noqa: BLE001 — registry problems must not block boot
            logger.exception("Scraper registry health check failed")
        log_with(
            logger, 20, "QBIT API started",
            env=settings.QBIT_ENV, data_dir=str(settings.data_dir),
        )
        try:
            yield
        finally:
            await app.state.db.close()
            await app.state.redis.close()
            log_with(logger, 20, "QBIT API stopped")

    app = FastAPI(
        title="QBIT Connect API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.QBIT_ENV != "production" else None,
        redoc_url=None,
    )

    # --- state ---------------------------------------------------------------
    app.state.settings = settings
    app.state.db = db or DatabaseManager(settings)
    app.state.storage = StorageService(settings)
    app.state.redis = RedisManager(settings)
    app.state.audit = AuditService()
    app.state.files = FileService(app.state.storage, app.state.audit)
    app.state.health = HealthService(app.state.db, app.state.storage, app.state.redis)
    app.state.login_limiter = SlidingWindowRateLimiter(
        max_events=settings.QBIT_RATE_LIMIT_LOGIN_PER_MIN, per_seconds=60.0
    )
    app.state.maintenance_mode = False

    # --- scraping engine (Phase 3) --------------------------------------------
    from app.scrapers.bootstrap import register_builtin_actors
    from app.services.marketing.providers import build_provider_registry
    from app.services.scraping.queue import build_queue_backend
    from app.services.scraping.registry import ActorRegistry

    app.state.scraper_registry = register_builtin_actors(ActorRegistry(), settings)
    app.state.queue = build_queue_backend(settings, app.state.redis)
    # Phase 5: marketing provider registry (mock provider gated to test envs)
    app.state.marketing_providers = build_provider_registry(settings)

    # --- middleware (order matters: outermost first) ---------------------------
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(MaintenanceMiddleware)
    app.add_middleware(RequestContextMiddleware)

    origins = settings.cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,  # never "*" — validated in production config
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    # --- error handling --------------------------------------------------------
    register_error_handlers(app)

    # --- routers ----------------------------------------------------------------
    from app.api.v1 import auth, files, leads, roles, users
    from app.api.v1 import health as health_routes
    from app.api.v1 import campaigns as campaigns_routes
    from app.api.v1 import connections as connections_routes
    from app.api.v1 import scrape_jobs as scrape_jobs_routes
    from app.api.v1 import scrapers as scrapers_routes
    from app.api.v1 import sending_accounts as sending_accounts_routes
    from app.api.v1 import settings as settings_routes
    from app.api.v1 import suppression as suppression_routes
    from app.api.v1 import templates as templates_routes
    from app.api.v1 import webhooks as webhooks_routes

    api_v1_prefix = "/api/v1"
    app.include_router(health_routes.router)  # /health, /health/database, ...
    app.include_router(auth.router, prefix=api_v1_prefix)
    app.include_router(users.router, prefix=api_v1_prefix)
    app.include_router(roles.router, prefix=api_v1_prefix)
    app.include_router(settings_routes.router, prefix=api_v1_prefix)
    app.include_router(files.router, prefix=api_v1_prefix)
    app.include_router(scrapers_routes.router, prefix=api_v1_prefix)
    app.include_router(scrape_jobs_routes.router, prefix=api_v1_prefix)
    app.include_router(leads.router, prefix=api_v1_prefix)
    app.include_router(campaigns_routes.router, prefix=api_v1_prefix)
    app.include_router(templates_routes.router, prefix=api_v1_prefix)
    app.include_router(sending_accounts_routes.router, prefix=api_v1_prefix)
    app.include_router(suppression_routes.router, prefix=api_v1_prefix)
    app.include_router(connections_routes.router, prefix=api_v1_prefix)
    app.include_router(webhooks_routes.router, prefix=api_v1_prefix)

    # --- operator UI (cookie-authenticated server-rendered pages) ---------------
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import RedirectResponse

    from app.ui import UiRedirect, router as ui_router
    from app.ui.campaigns import router as campaigns_ui_router
    from app.ui.connections import router as connections_ui_router
    from app.ui.leads import router as leads_ui_router

    app.include_router(ui_router)
    app.include_router(leads_ui_router)
    app.include_router(campaigns_ui_router)
    app.include_router(connections_ui_router)

    async def _ui_redirect_handler(request: Request, exc: UiRedirect):
        return RedirectResponse(url=exc.url, status_code=303)

    app.add_exception_handler(UiRedirect, _ui_redirect_handler)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.get("/api/v1", include_in_schema=False)
    async def api_root(request: Request):
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "name": "QBIT Connect API",
                    "version": __version__,
                    "request_id": request_context.get_request_id(),
                },
            }
        )

    return app
