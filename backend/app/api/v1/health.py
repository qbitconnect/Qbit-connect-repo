"""Health endpoints (Brief §16, §17) — no credentials or internal secrets exposed."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def overall_health(request: Request):
    """Aggregate health: database + storage + redis."""
    payload, http_code = await request.app.state.health.overall()
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=http_code, content=payload)


@router.get("/database")
async def database_health(request: Request):
    from fastapi.responses import JSONResponse

    payload = await request.app.state.health.database()
    status = 200 if payload["status"] == "online" else 503
    return JSONResponse(status_code=status, content=payload)


@router.get("/storage")
async def storage_health(request: Request):
    from fastapi.responses import JSONResponse

    payload = await request.app.state.health.storage()
    status = 200 if payload["status"] == "online" else 503
    return JSONResponse(status_code=status, content=payload)


@router.get("/redis")
async def redis_health(request: Request):
    from fastapi.responses import JSONResponse

    payload = await request.app.state.health.redis()
    # `disabled` is a valid, healthy state for a local-first deployment.
    status = 503 if payload["status"] == "unavailable" else 200
    return JSONResponse(status_code=status, content=payload)
