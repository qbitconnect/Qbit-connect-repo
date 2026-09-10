"""QBIT ACTOR PLATFORM — UI (spec §24/§41): an internal Apify-style console.

    GET  /actors                 actor catalog (schema-driven cards + health)
    GET  /datasets               dataset list
    GET  /datasets/{id}          dataset browser (search/sort/paginate/export)
    GET  /tasks                  saved configurations (run/duplicate/delete)
    GET  /run-webhooks           run-lifecycle webhook subscriptions
    GET  /run-webhooks/{id}      delivery log
    GET  /storage                KV store + request queues (§13/§14)
    GET  /api-docs               REST API reference (spec §23)

The catalog page is GENERATED FROM ACTOR METADATA (spec §3: no per-actor
hard-coded pages). Actions POST to server routes that call the same services
as the REST API; every POST re-checks the UI permission.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.actor_platform import ActorDataset, ActorTask, RunWebhook
from app.services.scraping.datasets import DatasetService
from app.services.scraping.health_monitor import ActorStats, HealthMonitor
from app.services.scraping.storage_services import KVStore, RequestQueue
from app.services.scraping.run_webhooks import RUN_EVENTS, RunWebhookService
from app.ui import _ctx, require_ui_permission, templates, ui_user_for

scraping_view = ui_user_for("scraping.view")
scraping_run = require_ui_permission("scraping.run")
scraping_export = require_ui_permission("scraping.export")
scraping_manage = require_ui_permission("scraping.manage")

router = APIRouter(tags=["actor-platform-ui"])

CATEGORY_LABELS = {
    "business_leads": "Business Leads",
    "website": "Website",
    "directory": "Directory",
    "public_data": "Public Data",
    "universal": "Universal",
    "email": "Email",
    "social_media": "Social Media",
    "ads": "Ads",
    "ecommerce": "E-commerce",
}


# ------------------------------------------------------------------ catalog
@router.get("/actors", response_class=HTMLResponse)
async def actors_catalog(
    request: Request,
    user: Annotated[object, Depends(scraping_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
    q: str = Query(default="", max_length=200),
    category: str = Query(default="", max_length=40),
):
    registry = request.app.state.scraper_registry
    stats = ActorStats(session)
    monitor = HealthMonitor(session)
    cards = []
    counts: dict[str, int] = {}
    for slug in registry.discover():
        entry = registry.entry(slug)
        actor = entry.actor
        meta = actor.metadata()
        counts[meta["category"]] = counts.get(meta["category"], 0) + 1
        if category and meta["category"] != category:
            continue
        haystack = f"{meta['name']} {meta['description']} {' '.join(meta['capabilities'])}".lower()
        if q and q.lower() not in haystack and q.lower() != slug:
            continue
        card = {
            "id": slug,
            "name": meta["name"],
            "version": meta["version"],
            "description": meta["description"],
            "category": meta["category"],
            "category_label": CATEGORY_LABELS.get(meta["category"], meta["category"]),
            "capabilities": meta["capabilities"][:4],
            "status": entry.public_status.value,
            "stats": await stats.per_actor(slug),
            "health": await monitor.latest(slug),
        }
        cards.append(card)
    platform = await stats.platform()
    return templates.TemplateResponse(
        request, "actor_platform/actors.html",
        _ctx(request, user, actors=cards, q=q, category=category,
             categories=sorted(counts), platform=platform),
    )


# ------------------------------------------------------------------ datasets
@router.get("/datasets", response_class=HTMLResponse)
async def datasets_page(
    request: Request,
    user: Annotated[object, Depends(scraping_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
):
    svc = DatasetService(session)
    limit = 20
    datasets, total = await svc.list(limit=limit, offset=(page - 1) * limit)
    pages = max(1, -(-total // limit))
    rows = []
    for ds in datasets:
        rows.append({
            "dataset": ds,
            "actor_name": _actor_name(request, ds.actor_id),
        })
    return templates.TemplateResponse(
        request, "actor_platform/datasets.html",
        _ctx(request, user, rows=rows, total=total, page=page, pages=pages),
    )


def _actor_name(request: Request, actor_id: str) -> str:
    try:
        return request.app.state.scraper_registry.get(actor_id).name
    except KeyError:
        return actor_id


@router.get("/datasets/{dataset_id}", response_class=HTMLResponse)
async def dataset_detail(
    dataset_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(scraping_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
    q: str = Query(default="", max_length=200),
    sort: str = Query(default="idx", max_length=100),
    order: str = Query(default="asc", max_length=4),
    page: int = Query(default=1, ge=1),
):
    dataset = await session.get(ActorDataset, dataset_id)
    if dataset is None:
        raise HTTP_404()
    svc = DatasetService(session)
    limit = 25
    items, total = await svc.items_page(
        dataset_id, search=q or None,
        sort_field=sort if sort != "idx" else None,
        sort_dir="desc" if order == "desc" else "asc",
        offset=(page - 1) * limit, limit=limit,
    )
    pages = max(1, -(-total // limit))
    return templates.TemplateResponse(
        request, "actor_platform/dataset_detail.html",
        _ctx(request, user, dataset=dataset,
             actor_name=_actor_name(request, dataset.actor_id),
             items=items, total=total, q=q, sort=sort, order=order,
             page=page, pages=pages, limit=limit),
    )


def HTTP_404():
    from app.core.errors import NotFoundError

    return NotFoundError("Dataset not found")


@router.post("/datasets/{dataset_id}/export", response_class=HTMLResponse)
async def dataset_export(
    dataset_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(scraping_export)],
    session: Annotated[AsyncSession, Depends(get_db)],
    format: str = Form("csv"),
):
    dataset = await session.get(ActorDataset, dataset_id)
    if dataset is None:
        raise HTTP_404()
    svc = DatasetService(session)
    try:
        filename, content = await svc.export(dataset, format)
    except ValueError:
        raise HTTP_404()
    from urllib.parse import quote

    media = {
        "json": "application/json", "jsonl": "application/x-ndjson",
        "csv": "text/csv", "xml": "application/xml", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return _download(content, filename, media.get(format, "application/octet-stream"))


def _download(content: bytes, filename: str, media_type: str):
    from fastapi import Response as FastAPIResponse
    from urllib.parse import quote

    return FastAPIResponse(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ------------------------------------------------------------------ tasks
@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    user: Annotated[object, Depends(scraping_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await session.execute(
        select(ActorTask).order_by(ActorTask.updated_at.desc()).limit(100)
    )).scalars().all()
    registry = request.app.state.scraper_registry
    actors = [{"id": slug, "name": registry.get(slug).name} for slug in registry.discover()]
    cards = [{
        "task": t,
        "actor_name": _actor_name(request, t.actor_id),
        "input_hint": _input_hint(t.input or {}),
    } for t in rows]
    return templates.TemplateResponse(
        request, "actor_platform/tasks.html",
        _ctx(request, user, tasks=cards, actors=actors),
    )


def _input_hint(data: dict) -> str:
    for key in ("url", "keyword", "username", "profile_url", "city", "search_url", "company_slug"):
        if data.get(key):
            return f"{key}: {str(data[key])[:60]}"
    return "saved input"


@router.post("/tasks/create", response_class=HTMLResponse)
async def task_create(
    request: Request,
    user: Annotated[object, Depends(scraping_run)],
    session: Annotated[AsyncSession, Depends(get_db)],
    actor_id: str = Form(...),
    name: str = Form(...),
    input_json: str = Form("{}"),
    config_json: str = Form("{}"),
):
    import json as _json

    from app.services.scraping.engine import sanitize_job_config

    entry = request.app.state.scraper_registry.entry(actor_id)
    if entry is None:
        raise HTTP_404()
    try:
        input_data = _json.loads(input_json or "{}")
        config = sanitize_job_config(_json.loads(config_json or "{}"))
    except ValueError:
        return RedirectResponse("/tasks?error=invalid+JSON+input", status_code=303)
    report = entry.actor.validate_input(input_data)
    if not report.valid:
        return RedirectResponse("/tasks?error=" + _urlencode_error(report.errors), status_code=303)
    task = ActorTask(
        actor_id=actor_id, name=name.strip()[:200],
        input=report.normalized_input, config=config, created_by=user.id,
    )
    session.add(task)
    await session.commit()
    return RedirectResponse("/tasks", status_code=303)


def _urlencode_error(errors: dict) -> str:
    from urllib.parse import urlencode

    return urlencode({"e": "; ".join(f"{k}: {v}" for k, v in errors.items())[:180]})


@router.post("/tasks/{task_id}/run", response_class=HTMLResponse)
async def task_run(
    task_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(scraping_run)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    from datetime import datetime, timezone

    from app.services.scraping.engine import JobEngine

    task = await session.get(ActorTask, task_id)
    if task is None:
        raise HTTP_404()
    entry = request.app.state.scraper_registry.entry(task.actor_id)
    if entry is None or not entry.enabled:
        return RedirectResponse("/tasks?error=actor+unavailable", status_code=303)
    engine = JobEngine(session, request.app.state.queue)
    job = await engine.create_job(
        actor=entry.actor, validated_input=task.input or {},
        config=task.config or {}, created_by=user.id,
        name=task.name, trigger="TASK", task_id=task.id,
    )
    task.run_count = (task.run_count or 0) + 1
    task.last_run_at = datetime.now(timezone.utc)
    task.last_job_id = job.id
    await session.commit()
    return RedirectResponse(f"/scraping/jobs/{job.id}", status_code=303)


@router.post("/tasks/{task_id}/duplicate", response_class=HTMLResponse)
async def task_duplicate(
    task_id: uuid.UUID,
    user: Annotated[object, Depends(scraping_run)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    task = await session.get(ActorTask, task_id)
    if task is None:
        raise HTTP_404()
    copy = ActorTask(
        actor_id=task.actor_id, name=f"{task.name} (copy)"[:200],
        description=task.description, input=task.input, config=task.config,
        created_by=getattr(user, "id", None),
    )
    session.add(copy)
    await session.commit()
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{task_id}/delete", response_class=HTMLResponse)
async def task_delete(
    task_id: uuid.UUID,
    user: Annotated[object, Depends(scraping_run)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    task = await session.get(ActorTask, task_id)
    if task is not None:
        await session.delete(task)
        await session.commit()
    return RedirectResponse("/tasks", status_code=303)


# ------------------------------------------------------------------ webhooks
@router.get("/run-webhooks", response_class=HTMLResponse)
async def webhooks_page(
    request: Request,
    user: Annotated[object, Depends(scraping_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    hooks = (await session.execute(
        select(RunWebhook).order_by(RunWebhook.created_at.desc())
    )).scalars().all()
    from app.models.actor_platform import RunWebhookDelivery

    counts = dict((await session.execute(
        select(RunWebhookDelivery.webhook_id, func.count(RunWebhookDelivery.id))
        .group_by(RunWebhookDelivery.webhook_id)
    )).all())
    delivered = dict((await session.execute(
        select(RunWebhookDelivery.webhook_id, func.count(RunWebhookDelivery.id))
        .where(RunWebhookDelivery.status == "DELIVERED")
        .group_by(RunWebhookDelivery.webhook_id)
    )).all())
    cards = [{
        "hook": h,
        "actor_name": _actor_name(request, h.actor_id) if h.actor_id else "All actors",
        "deliveries": int(counts.get(h.id, 0)),
        "delivered": int(delivered.get(h.id, 0)),
    } for h in hooks]
    return templates.TemplateResponse(
        request, "actor_platform/webhooks.html",
        _ctx(request, user, hooks=cards, events=list(RUN_EVENTS)),
    )


@router.post("/run-webhooks/create", response_class=HTMLResponse)
async def webhook_create(
    request: Request,
    user: Annotated[object, Depends(scraping_manage)],
    session: Annotated[AsyncSession, Depends(get_db)],
    name: str = Form(...),
    url: str = Form(...),
    secret: str = Form(""),
    events: str = Form(""),
    actor_id: str = Form(""),
):
    if not url.lower().startswith(("http://", "https://")):
        return RedirectResponse("/run-webhooks?error=url+must+be+http(s)", status_code=303)
    chosen = [e.strip() for e in events.split(",") if e.strip()] or list(RUN_EVENTS)
    hook = RunWebhook(
        name=name.strip()[:200], url=url.strip()[:1000],
        secret=secret.strip()[:300] or uuid.uuid4().hex + uuid.uuid4().hex,
        events=chosen, actor_id=actor_id.strip() or None,
        created_by=getattr(user, "id", None),
    )
    session.add(hook)
    await session.commit()
    return RedirectResponse("/run-webhooks", status_code=303)


@router.get("/run-webhooks/{hook_id}", response_class=HTMLResponse)
async def webhook_detail(
    hook_id: uuid.UUID,
    request: Request,
    user: Annotated[object, Depends(scraping_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    hook = await session.get(RunWebhook, hook_id)
    if hook is None:
        raise HTTP_404()
    rows = await RunWebhookService(session).deliveries(hook_id, limit=50)
    return templates.TemplateResponse(
        request, "actor_platform/webhook_detail.html",
        _ctx(request, user, hook=hook, deliveries=rows,
             actor_name=_actor_name(request, hook.actor_id) if hook.actor_id else "All actors"),
    )


@router.post("/run-webhooks/{hook_id}/toggle", response_class=HTMLResponse)
async def webhook_toggle(
    hook_id: uuid.UUID,
    user: Annotated[object, Depends(scraping_manage)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    from datetime import datetime, timezone

    hook = await session.get(RunWebhook, hook_id)
    if hook is None:
        raise HTTP_404()
    hook.enabled = not hook.enabled
    hook.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return RedirectResponse("/run-webhooks", status_code=303)


@router.post("/run-webhooks/{hook_id}/delete", response_class=HTMLResponse)
async def webhook_delete(
    hook_id: uuid.UUID,
    user: Annotated[object, Depends(scraping_manage)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    hook = await session.get(RunWebhook, hook_id)
    if hook is not None:
        await session.delete(hook)
        await session.commit()
    return RedirectResponse("/run-webhooks", status_code=303)


# ------------------------------------------------------------------ storage
@router.get("/storage", response_class=HTMLResponse)
async def storage_page(
    request: Request,
    user: Annotated[object, Depends(scraping_view)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    kv_rows, kv_total = await KVStore(session).list(limit=100)
    rq = RequestQueue(session)
    queues = [await rq.stats(q["queue_name"]) for q in await rq.list_queues()]
    return templates.TemplateResponse(
        request, "actor_platform/storage.html",
        _ctx(request, user, kv_rows=kv_rows, kv_total=kv_total, queues=queues),
    )


# ------------------------------------------------------------------ api docs
@router.get("/api-docs", response_class=HTMLResponse)
async def api_docs(
    request: Request,
    user: Annotated[object, Depends(scraping_view)],
):
    endpoints = [
        ("GET", "/api/v1/actors", "Actor catalog with metadata, run statistics and latest health", "scraping.view"),
        ("GET", "/api/v1/actors/{slug}", "Actor detail: input schema, output fields, capabilities, examples", "scraping.view"),
        ("POST", "/api/v1/actors/{slug}/validate", "Strict input validation before enqueueing", "scraping.view"),
        ("POST", "/api/v1/actors/{slug}/runs", "Create + enqueue a run (spec: POST /api/actors/{actor}/runs)", "scraping.run"),
        ("GET", "/api/v1/actors/{slug}/runs", "Run history for one actor", "scraping.view"),
        ("GET", "/api/v1/runs", "Run list (filters: actor_id, status, trigger)", "scraping.view"),
        ("GET", "/api/v1/runs/{run_id}", "Run detail incl. dataset id", "scraping.view"),
        ("POST", "/api/v1/runs/{run_id}/pause|resume|cancel|retry", "Run control (spec §23)", "scraping.pause / cancel / run"),
        ("GET", "/api/v1/runs/{run_id}/logs", "Structured run logs", "scraping.view"),
        ("GET", "/api/v1/datasets", "Dataset list", "scraping.view"),
        ("GET", "/api/v1/datasets/{id}/items", "Search / filter / sort / paginate dataset rows", "scraping.view"),
        ("POST", "/api/v1/datasets/{id}/export", "Export ALL | SELECTED(ids) | FILTERED — json/jsonl/csv/xlsx/xml", "scraping.export"),
        ("GET", "/api/v1/datasets/{id}/changes", "Snapshot change detection (new/unchanged/modified/stopped/resumed)", "scraping.view"),
        ("GET|POST", "/api/v1/tasks", "Saved actor configurations", "scraping.view / run"),
        ("POST", "/api/v1/tasks/{id}/run", "Run a saved Task (same input each time)", "scraping.run"),
        ("POST", "/api/v1/tasks/{id}/duplicate", "Duplicate a Task", "scraping.run"),
        ("GET|POST|PATCH|DELETE", "/api/v1/run-webhooks", "Run-lifecycle webhook subscriptions", "scraping.view / manage"),
        ("GET", "/api/v1/run-webhooks/{id}/deliveries", "Delivery attempts + signatures bookkeeping", "scraping.view"),
        ("POST", "/api/v1/run-webhooks/{id}/test", "Queue a signed TEST delivery", "scraping.manage"),
        ("GET|PUT|DELETE", "/api/v1/storage/kv/{scope}/{key}", "Key-value storage (actor state)", "scraping.view / manage"),
        ("GET", "/api/v1/storage/queues", "Persistent request queues", "scraping.view"),
        ("GET", "/api/v1/scrape-schedules", "Schedules (ONCE/INTERVAL/DAILY + cron-style aliases)", "scraping.view"),
    ]
    return templates.TemplateResponse(
        request, "actor_platform/api_docs.html",
        _ctx(request, user, endpoints=endpoints),
    )
