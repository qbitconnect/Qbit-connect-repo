"""Health endpoints (Brief §16, §17) — no credentials or internal secrets exposed.

Phase 12 (audit M2/M7, brief §32):
- /health/live   — LIVENESS: the application process is alive. Zero dependency
                   probes, so a transient DB/Redis blip never restarts the
                   container (Docker HEALTHCHECK uses this endpoint).
- /health/ready  — READINESS: the application can safely serve traffic
                   (aggregate of database + storage + redis).
- /health        — legacy aggregate (same payload as /health/ready).
- /health/database|storage|redis — component details.
- The absolute storage path is no longer exposed by any public health
  payload (audit M7); operators get it from the permission-gated admin ops
  snapshot instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health", tags=["health"])


def _public(payload: dict) -> dict:
    """Strip internal details (absolute filesystem paths) from health payloads —
    including the nested `services.*` blocks of the aggregate response."""

    def scrub(node: dict) -> dict:
        out = {}
        for key, value in node.items():
            if key == "path":
                continue
            if isinstance(value, dict):
                out[key] = scrub(value)
            else:
                out[key] = value
        return out

    return scrub(payload)


@router.get("/live")
async def liveness():
    """Liveness probe — process is alive; NO dependency checks by design."""
    return JSONResponse(status_code=200, content={"status": "alive"})


@router.get("/ready")
async def readiness(request: Request):
    """Readiness probe — safe to serve traffic (aggregate dependency check)."""
    payload, http_code = await request.app.state.health.overall()
    return JSONResponse(status_code=http_code, content=_public(payload))


@router.get("")
async def overall_health(request: Request):
    """Aggregate health: database + storage + redis (legacy alias of /ready)."""
    payload, http_code = await request.app.state.health.overall()
    return JSONResponse(status_code=http_code, content=_public(payload))


@router.get("/database")
async def database_health(request: Request):
    payload = await request.app.state.health.database()
    status = 200 if payload["status"] == "online" else 503
    return JSONResponse(status_code=status, content=_public(payload))


@router.get("/storage")
async def storage_health(request: Request):
    payload = _public(await request.app.state.health.storage())
    status = 200 if payload["status"] == "online" else 503
    return JSONResponse(status_code=status, content=payload)


@router.get("/redis")
async def redis_health(request: Request):
    payload = await request.app.state.health.redis()
    # `disabled` is a valid, healthy state for a local-first deployment.
    status = 503 if payload["status"] == "unavailable" else 200
    return JSONResponse(status_code=status, content=payload)
