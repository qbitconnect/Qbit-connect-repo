"""Operator UI — server-rendered scraping pages (brief §33–§37).

Cookie-session authentication (HttpOnly + SameSite=Lax) wrapping the same JWT
used by the API; the API itself stays Bearer-only. The UI is a thin client
over the same services: forms POST to /ui endpoints that call JobEngine —
never scraper logic in the browser (§53).

Pages:
    GET  /login, POST /login, POST /logout
    GET  /scraping                     scraper cards (search)
    GET  /scraping/{actor_id}          detail + run form (+ validate)
    GET  /scraping/jobs                job list (filters)
    GET  /scraping/jobs/{id}           job detail (progress, log, results)
    GET  /scraping/jobs/{id}/live      JSON poll for the detail page
    POST /scraping/jobs/{id}/pause|resume|cancel|retry
    GET  /scraping/jobs/{id}/export    CSV/XLSX/JSON download (ExportService)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_client_ip, get_db, get_user_agent
from app.core.errors import QBITError
from app.core.security import create_access_token, decode_access_token, verify_password
from app.models.enterprise import Invitation, UserSession
from app.models.scrape import JobStatus
from app.models.user import User
from app.services import login_guard
from app.services.export import ExportService
from app.services.leads import LeadService
from app.services.scraping.engine import JobEngine, sanitize_job_config

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["str"] = str  # str(x)[:8] slicing in templates
router = APIRouter(tags=["ui"])

COOKIE_NAME = "qbit_session"


class UiRedirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


async def _resolve_user(request: Request, session: AsyncSession) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    settings = request.app.state.settings
    try:
        payload = decode_access_token(token, secret_key=settings.QBIT_SECRET_KEY)
    except Exception:  # noqa: BLE001 — expired/invalid cookie = anonymous
        return None
    try:
        uid = uuid.UUID(payload.get("sub", ""))
    except ValueError:
        return None
    user = await session.get(User, uid)
    if user is None or not user.is_active:
        return None
    # Phase 12 (audit M4): UI logins are now first-class revocable sessions —
    # the same server-side checks the API enforces in deps.py. An admin who
    # revokes a session (or forces logout) evicts the UI cookie holder too.
    revoked_before = user.tokens_revoked_before
    if revoked_before is not None:
        issued = payload.get("iat")
        if isinstance(issued, (int, float)):
            from datetime import datetime as _dt, timezone as _tz

            issued_dt = _dt.fromtimestamp(issued, tz=_tz.utc)
            rb = revoked_before if revoked_before.tzinfo else revoked_before.replace(tzinfo=_tz.utc)
            if issued_dt < rb:
                return None
    jti = payload.get("jti")
    if jti:
        from datetime import datetime as _dt, timezone as _tz

        row = await session.scalar(
            select(UserSession).where(UserSession.jti == jti)
        )
        if row is not None and not row.is_live:
            return None
        if row is not None:
            now = _dt.now(_tz.utc)
            last = row.last_seen_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=_tz.utc)
            if last is None or (now - last).total_seconds() >= 60:
                row.last_seen_at = now
                await session.commit()
    return user


async def ui_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Require a logged-in user; anonymous → login page (scraping.view)."""
    user = await _resolve_user(request, session)
    if user is None:
        raise UiRedirect("/login?next=" + request.url.path)
    from app.services import rbac as rbac_service

    perms = await rbac_service.load_user_permissions(session, user.id)
    request.state.ui_permissions = perms
    if "scraping.view" not in perms:
        raise UiRedirect("/403")
    return user


def require_ui_permission(code: str):
    async def dep(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        user = await ui_user(request, session)  # also loads permissions
        if code not in (request.state.ui_permissions or set()):
            raise UiRedirect("/403")
        return user
    return dep


def ui_user_for(required_permission: str | None = None):
    """Parameterized UI auth dependency (Phase 4: leads.* pages)."""

    async def dep(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        user = await _resolve_user(request, session)
        if user is None:
            raise UiRedirect("/login?next=" + request.url.path)
        from app.services import rbac as rbac_service

        perms = await rbac_service.load_user_permissions(session, user.id)
        request.state.ui_permissions = perms
        if required_permission and required_permission not in perms:
            raise UiRedirect("/403")
        return user

    return dep


def _ctx(request: Request, user: User | None, **extra) -> dict:
    # runtime env label for the topbar badge (honest indicator, never hardcoded)
    env = "DEV"
    try:
        raw = str(getattr(request.app.state.settings, "QBIT_ENV", "development")).lower()
        env = {"development": "DEV", "production": "PROD", "staging": "STAGING",
               "test": "TEST"}.get(raw, raw.upper()[:8])
    except Exception:  # noqa: BLE001 — badge must never break a page render
        pass
    return {
        "request": request,
        "user": user,
        "year": datetime.now(timezone.utc).year,
        "env_label": env,
        # Phase 11: nav gating (UI-only concern; the API enforces for real)
        "perms": getattr(request.state, "ui_permissions", None) or set(),
        **extra,
    }


# --------------------------------------------------------------------- auth
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/scraping"):
    return templates.TemplateResponse(
        request, "login.html", _ctx(request, None, next=next, error=None)
    )


@router.post("/login")
async def login_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/scraping"),
):
    from app.services import rbac as rbac_service

    settings = request.app.state.settings
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/scraping"

    # Phase 12 (audit H7): the UI form route is rate limited EXACTLY like the
    # API login (per-IP sliding window) — it used to be an unthrottled bypass.
    limiter = request.app.state.login_limiter
    if not limiter.check(get_client_ip(request)):
        retry_after = int(round(limiter.retry_after_seconds(get_client_ip(request)))) or 60
        response = templates.TemplateResponse(
            request, "login.html",
            _ctx(request, None, next=safe_next, error="Too many login attempts. Try again shortly."),
            status_code=429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    def _deny(message: str, status: int = 401):
        return templates.TemplateResponse(
            request, "login.html",
            _ctx(request, None, next=safe_next, error=message),
            status_code=status,
        )

    user = await session.scalar(
        select(User).where(User.email == email.strip().lower())
    )
    # Constant-shape for wrong credentials AND locked accounts (audit M11):
    # lockout must not become a password-confirmation oracle.
    if user is None:
        verify_password(password, "$argon2id$invalidplaceholderhashvalue")
        return _deny("Invalid email or password")
    if not verify_password(password, user.password_hash):
        await login_guard.register_failure(session, user, settings)
        return _deny("Invalid email or password")
    if login_guard.is_locked(user):
        return _deny("Invalid email or password")
    await login_guard.register_success(session, user)

    if not user.is_active or user.effective_status in ("SUSPENDED", "DEACTIVATED"):
        # only reachable WITH the correct password — no enumeration channel
        return _deny("This account cannot sign in", status=403)
    perms = await rbac_service.load_user_permissions(session, user.id)
    if "scraping.view" not in perms:
        return _deny("This account cannot sign in", status=403)

    token, expires_at = create_access_token(
        subject=str(user.id),
        secret_key=settings.QBIT_SECRET_KEY,
        ttl_minutes=settings.QBIT_SESSION_TTL_MINUTES,
    )
    # Phase 12 (audit M4): register a revocable server-side session for the
    # UI login (previously UI sessions were invisible to the Sessions admin).
    try:
        jti = decode_access_token(token, secret_key=settings.QBIT_SECRET_KEY).get("jti")
        if jti:
            session.add(
                UserSession(
                    user_id=user.id,
                    jti=jti,
                    ip_address=get_client_ip(request),
                    user_agent=get_user_agent(request),
                    expires_at=expires_at,
                    last_seen_at=datetime.now(timezone.utc),
                )
            )
            user.last_login_at = datetime.now(timezone.utc)
            await session.commit()
    except Exception:  # noqa: BLE001 — session bookkeeping must not block login
        await session.rollback()
    response = RedirectResponse(url=safe_next, status_code=303)
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="lax",
        secure=settings.cookie_secure,  # Phase 12 (audit M4)
        max_age=settings.QBIT_SESSION_TTL_MINUTES * 60,
        path="/",
    )
    return response


@router.post("/logout")
async def logout(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    """Phase 12 (audit M4): revoke the server-side session, not just the cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            payload = decode_access_token(
                token, secret_key=request.app.state.settings.QBIT_SECRET_KEY
            )
            jti = payload.get("jti")
            if jti:
                row = await session.scalar(
                    select(UserSession).where(UserSession.jti == jti)
                )
                if row is not None and row.revoked_at is None:
                    row.revoked_at = datetime.now(timezone.utc)
                    row.revoked_reason = "LOGOUT"
                    await session.commit()
        except Exception:  # noqa: BLE001 — logout must never fail
            pass
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/403", response_class=HTMLResponse)
async def forbidden(request: Request):
    return templates.TemplateResponse(
        request, "403.html", _ctx(request, None), status_code=403
    )


# --------------------------------------------------------------------- home
@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(ui_user_for())],
):
    """Command center (redesign brief: premium internal home).

    Read-only: every number is a real count from the same sources the
    section pages use — nothing estimated, nothing faked. Missing data
    renders honest zero/empty states.
    """
    from sqlalchemy import func as sa_func

    from app.models.automation import Workflow
    from app.models.marketing import Campaign
    from app.models.messaging import Conversation
    from app.models.scrape import JobStatus, ScrapeJob

    from .leads import workspace as lead_workspace

    async def _scalar(stmt) -> int:
        return int(await session.scalar(stmt) or 0)

    # leads — same source as the leads workspace KPI row
    leads_total = await lead_workspace.count_all(session)

    # scraping jobs — same source as Jobs (JobEngine.list_jobs page 1)
    engine = JobEngine(session, request.app.state.queue)
    jobs_running = await _scalar(
        select(sa_func.count(ScrapeJob.id)).select_from(ScrapeJob)
        .where(ScrapeJob.status == JobStatus.RUNNING.value)
    )
    jobs_failed_24h = await _scalar(
        select(sa_func.count(ScrapeJob.id)).select_from(ScrapeJob).where(
            ScrapeJob.status == JobStatus.FAILED.value,
            ScrapeJob.created_at >= datetime.now(timezone.utc) - timedelta(hours=24),
        )
    )
    recent_jobs, jobs_total = await engine.list_jobs(page=1, page_size=6)
    registry = request.app.state.scraper_registry
    recent_rows = []
    for job in recent_jobs:
        try:
            actor_name = registry.get(job.actor_id).name
        except KeyError:
            actor_name = job.actor_id
        recent_rows.append({"job": job, "actor_name": actor_name})

    # marketing — campaigns by lifecycle group (real counts)
    campaigns_total = await _scalar(select(sa_func.count(Campaign.id)).select_from(Campaign))
    campaigns_active = await _scalar(
        select(sa_func.count(Campaign.id)).select_from(Campaign)
        .where(Campaign.status.in_(["RUNNING", "SCHEDULED", "QUEUED"]))
    )

    # inbox — open threads awaiting a reply
    inbox_open = await _scalar(
        select(sa_func.count(Conversation.id)).select_from(Conversation)
        .where(Conversation.status.in_(["OPEN", "WAITING", "PENDING"]))
    )

    # automation — published/active workflows
    workflows_total = await _scalar(select(sa_func.count(Workflow.id)).select_from(Workflow))
    workflows_active = await _scalar(
        select(sa_func.count(Workflow.id)).select_from(Workflow)
        .where(Workflow.status == "ACTIVE")
    )

    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")

    return templates.TemplateResponse(
        request, "home.html",
        _ctx(
            request, user,
            greeting=greeting,
            leads_total=leads_total,
            jobs_total=jobs_total,
            jobs_running=jobs_running,
            jobs_failed_24h=jobs_failed_24h,
            recent_jobs=recent_rows,
            campaigns_total=campaigns_total,
            campaigns_active=campaigns_active,
            inbox_open=inbox_open,
            workflows_total=workflows_total,
            workflows_active=workflows_active,
        ),
    )


# ----------------------------------------------------------------- scrapers
@router.get("/scraping", response_class=HTMLResponse)
async def scraping_home(
    request: Request,
    user: Annotated[User, Depends(ui_user)],
    q: str = Query(default="", max_length=100),
    category: str = Query(default="", max_length=50),
):
    registry = request.app.state.scraper_registry
    # Refresh health on view — all built-in health checks are local/fast (§49)
    try:
        await registry.health_check()
    except Exception:  # noqa: BLE001 — status display must not crash the page
        pass
    cards = []
    for actor_id in registry.discover():
        entry = registry.entry(actor_id)
        actor = registry.get(actor_id)
        meta = actor.metadata()
        cards.append(
            {
                **meta,
                "status": entry.public_status.value if entry else "REGISTERED",
                "status_detail": entry.detail if entry else None,
            }
        )
    cards_all = list(cards)  # unfiltered copy for the category pill counts
    if q:
        ql = q.lower()
        cards = [
            c for c in cards
            if ql in c["name"].lower() or ql in c["description"].lower()
            or ql in c["category"]
        ]
    # Category pills (UI-only filter over the registry listing, no data change)
    all_categories = sorted({
        c["category"].replace("_", " ").title() for c in cards_all
    })
    if category:
        wanted = category.lower()
        cards = [c for c in cards if c["category"].replace("_", " ").lower() == wanted]
    return templates.TemplateResponse(
        request, "scraping/index.html",
        _ctx(request, user, scrapers=cards, q=q, category=category,
             categories=all_categories, total_registered=len(cards_all))
    )


@router.get("/scraping/jobs", response_class=HTMLResponse)
async def jobs_list(
    request: Request,
    user: Annotated[User, Depends(ui_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    status: str = Query(default="", max_length=20),
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
):
    engine = JobEngine(session, request.app.state.queue)
    jobs, total = await engine.list_jobs(
        status=status or None, search=q or None, page=page, page_size=20
    )
    # enrich with actor names (registry lookup, no DB join needed)
    registry = request.app.state.scraper_registry
    rows = []
    for job in jobs:
        try:
            actor_name = registry.get(job.actor_id).name
        except KeyError:
            actor_name = job.actor_id
        rows.append({"job": job, "actor_name": actor_name})
    pages = max(1, -(-total // 20))
    return templates.TemplateResponse(
        request, "jobs/index.html",
        _ctx(request, user, jobs=rows, total=total, status=status, q=q,
             page=page, pages=pages,
             statuses=[s.value for s in JobStatus]),
    )


@router.get("/scraping/{actor_id}", response_class=HTMLResponse)
async def scraper_detail(
    actor_id: str,
    request: Request,
    user: Annotated[User, Depends(ui_user)],
):
    registry = request.app.state.scraper_registry
    try:
        entry = registry.entry(actor_id)
        actor = registry.get(actor_id)
    except KeyError:
        raise UiRedirect("/scraping")
    meta = actor.metadata()
    schema_props = meta["input_schema"].get("properties", {})
    required = set(meta["input_schema"].get("required", []))
    fields = []
    for name, prop in schema_props.items():
        fields.append(
            {
                "name": name,
                "type": prop.get("type", "string"),
                "required": name in required,
                "default": prop.get("default"),
                "minimum": prop.get("minimum"),
                "maximum": prop.get("maximum"),
            }
        )
    return templates.TemplateResponse(
        request, "scraping/detail.html",
        _ctx(request, user, actor=meta, status=entry.public_status.value,
             status_detail=entry.detail, fields=fields,
             form_values={}, errors={}),
    )


@router.post("/scraping/{actor_id}/validate")
async def scraper_validate(
    actor_id: str,
    request: Request,
    user: Annotated[User, Depends(ui_user)],
):
    registry = request.app.state.scraper_registry
    try:
        actor = registry.get(actor_id)
    except KeyError:
        return JSONResponse({"valid": False, "errors": {"actor": "unknown"}}, status_code=404)
    payload = await request.json()
    report = actor.validate_input(payload.get("input") or {})
    try:
        sanitize_job_config(payload.get("config") or {})
    except QBITError as exc:
        return JSONResponse(
            {"valid": False, "errors": {"config": exc.message}}, status_code=200
        )
    return JSONResponse(
        {"valid": report.valid, "errors": report.errors}, status_code=200
    )


@router.post("/scraping/{actor_id}/run")
async def scraper_run(
    actor_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("scraping.run"))],
):
    registry = request.app.state.scraper_registry
    entry = registry.entry(actor_id)
    if entry is None:
        raise UiRedirect("/scraping")
    if not entry.enabled:
        return templates.TemplateResponse(
            request, "403.html", _ctx(request, user, detail="This scraper is disabled"),
            status_code=403,
        )
    actor = entry.actor
    form = await request.form()
    input_data: dict = {}
    for key in form:
        if key.startswith("input__"):
            value = str(form[key]).strip()
            if value:
                input_data[key.removeprefix("input__")] = value
    # boolean checkboxes: absent = false
    schema_props = actor.input_schema.model_json_schema().get("properties", {})
    for name, prop in schema_props.items():
        if prop.get("type") == "boolean" and name not in input_data:
            input_data[name] = False
    config_data: dict = {}
    for key in ("max_pages", "max_records", "max_runtime_seconds", "request_timeout",
                "requests_per_second", "concurrency", "max_retries"):
        raw = str(form.get(f"config__{key}", "")).strip()
        if raw:
            try:
                config_data[key] = float(raw) if "." in raw else int(raw)
            except ValueError:
                pass
    if form.get("config__respect_robots") is not None:
        config_data["respect_robots"] = str(form["config__respect_robots"]) == "on"

    report = actor.validate_input(input_data)
    if not report.valid:
        return templates.TemplateResponse(
            request, "scraping/detail.html",
            _ctx(request, user,
                 actor=actor.metadata(), status=entry.public_status.value,
                 status_detail=entry.detail,
                 fields=_form_fields(actor), errors=report.errors,
                 form_values=input_data),
            status_code=422,
        )
    try:
        config = sanitize_job_config(config_data)
    except QBITError:
        config = {}

    engine = JobEngine(session, request.app.state.queue)
    job = await engine.create_job(
        actor=actor,
        validated_input=report.normalized_input,
        config=config,
        created_by=user.id,
    )
    return RedirectResponse(url=f"/scraping/jobs/{job.id}", status_code=303)


def _form_fields(actor) -> list[dict]:
    schema = actor.input_schema.model_json_schema()
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    return [
        {
            "name": name,
            "type": prop.get("type", "string"),
            "required": name in required,
            "default": prop.get("default"),
            "minimum": prop.get("minimum"),
            "maximum": prop.get("maximum"),
        }
        for name, prop in props.items()
    ]


# --------------------------------------------------------------------- jobs
@router.get("/scraping/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(
    job_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(ui_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    engine = JobEngine(session, request.app.state.queue)
    try:
        job = await engine.get_job(job_id)
    except Exception:
        raise UiRedirect("/scraping/jobs")
    registry = request.app.state.scraper_registry
    try:
        actor_name = registry.get(job.actor_id).name
        actor_meta = registry.get(job.actor_id).metadata()
    except KeyError:
        actor_name, actor_meta = job.actor_id, {}
    leads_page, total = await LeadService().list_for_job(session, job.id, page=1, page_size=20)
    perms = request.state.ui_permissions or set()
    end_ref = job.completed_at or job.cancelled_at or datetime.now(timezone.utc)
    elapsed_display = "—"
    if job.started_at:
        seconds = max(0, int((end_ref - job.started_at).total_seconds()))
        elapsed_display = f"{seconds // 60}m {seconds % 60}s"
    return templates.TemplateResponse(
        request, "jobs/detail.html",
        _ctx(request, user, job=job, actor_name=actor_name, actor_meta=actor_meta,
             leads=[lead.to_public_dict() for lead in leads_page], leads_total=total,
             elapsed_display=elapsed_display,
             can_pause="scraping.pause" in perms,
             can_cancel="scraping.cancel" in perms,
             can_retry="scraping.run" in perms,
             can_export="scraping.export" in perms),
    )


@router.get("/scraping/jobs/{job_id}/live")
async def job_live(
    job_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(ui_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    after_log: int = 0,
):
    """Lightweight poll endpoint: job counters + recent events (§35, §38)."""
    engine = JobEngine(session, request.app.state.queue)
    try:
        job = await engine.get_job(job_id)
    except Exception:
        return JSONResponse({"error": "not_found"}, status_code=404)
    from app.models.scrape import ScrapeJobEvent

    events_query = (
        select(ScrapeJobEvent)
        .where(ScrapeJobEvent.job_id == job.id)
        .order_by(ScrapeJobEvent.created_at.desc())
        .limit(40)
    )
    rows = (await session.scalars(events_query)).all()
    events = [
        {
            "at": e.created_at.strftime("%H:%M:%S") if e.created_at else "",
            "type": e.event_type,
            "message": e.message or "",
        }
        for e in reversed(rows)
    ]
    return JSONResponse(
        {
            "job": job.to_public_dict(),
            "events": events,
        }
    )


async def _job_control(
    request: Request,
    job_id: uuid.UUID,
    user: User,
    session: AsyncSession,
    action: str,
):
    engine = JobEngine(session, request.app.state.queue)
    try:
        job = await engine.get_job(job_id)
        updated = await getattr(engine, action)(job)
    except QBITError:
        job = None
        updated = None
    return RedirectResponse(url=f"/scraping/jobs/{job_id}", status_code=303)


@router.post("/scraping/jobs/{job_id}/pause")
async def job_pause(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("scraping.pause"))],
):
    return await _job_control(request, job_id, user, session, "pause")


@router.post("/scraping/jobs/{job_id}/resume")
async def job_resume(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("scraping.pause"))],
):
    return await _job_control(request, job_id, user, session, "resume")


@router.post("/scraping/jobs/{job_id}/cancel")
async def job_cancel(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("scraping.cancel"))],
):
    return await _job_control(request, job_id, user, session, "cancel")


@router.post("/scraping/jobs/{job_id}/retry")
async def job_retry(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("scraping.run"))],
):
    return await _job_control(request, job_id, user, session, "retry_job")


@router.get("/scraping/jobs/{job_id}/export")
async def job_export(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("scraping.export"))],
    format: str = Query(default="csv", pattern=r"^(csv|xlsx|json)$"),
):
    engine = JobEngine(session, request.app.state.queue)
    try:
        job = await engine.get_job(job_id)
    except Exception:
        raise UiRedirect("/scraping/jobs")
    if job.status not in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        raise UiRedirect(f"/scraping/jobs/{job_id}")

    leads_service = LeadService()
    rows: list[dict] = []
    page = 1
    while True:
        batch, _t = await leads_service.list_for_job(session, job.id, page=page, page_size=500)
        if not batch:
            break
        for lead in batch:
            d = lead.to_public_dict()
            rows.append({
                "business_name": d.get("business_name"), "email": d.get("email"),
                "phone": d.get("phone"), "website": d.get("website"),
                "address": d.get("address"), "city": d.get("city"),
                "state": d.get("state"), "country": d.get("country"),
                "category": d.get("category"), "source": d.get("source"),
                "source_url": d.get("source_url"), "scraped_at": d.get("scraped_at"),
            })
        page += 1

    exporter = ExportService(request.app.state.files)
    # Phase 11 §24: export files carry the caller's organization
    from app.services import authorization as authz
    from app.services import rbac as rbac_service

    _perms = await rbac_service.load_user_permissions(session, user.id)
    _ctx = await authz.resolve_context(session, user, _perms)
    record = await exporter.export(
        session, format_name=format, rows=rows,
        base_name=f"scrape-job-{str(job.id)[:8]}-results",
        created_by=user.id, organization_id=_ctx.organization_id,
        metadata={"job_id": str(job.id)},
    )
    root = request.app.state.storage.root_for("EXPORT")
    from app.core.path_safety import validate_storage_key

    path = validate_storage_key(record.path, root)
    data = path.read_bytes()
    return Response(
        content=data,
        media_type=_MEDIA.get(format, "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{record.name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


_MEDIA = {
    "csv": "text/csv",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


# ------------------------------------------- Phase 11: invite + notifications
@router.get("/invite", response_class=HTMLResponse)
async def invite_page(request: Request, token: str = Query(default="")):
    """Public invitation acceptance page (§5). No auth — the token is the secret."""
    return templates.TemplateResponse(
        request, "invite_accept.html",
        {"request": request, "token": token, "user": None, "perms": set(),
         "year": datetime.now(timezone.utc).year, "ok": None, "err": None},
    )


@router.post("/invite")
async def invite_accept(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Form()],
    password: Annotated[str, Form(min_length=8)],
    full_name: Annotated[str, Form(max_length=200)] = "",
):
    from app.core.security import hash_token_secret
    from app.models.enterprise import Organization, OrganizationMember, Team, TeamMember, utc_aware
    from app.models.rbac import Role, user_roles
    from datetime import datetime as _dt

    inv = await session.scalar(
        select(Invitation).where(Invitation.token_hash == hash_token_secret(token.strip()))
    )
    if inv is None:
        return templates.TemplateResponse(
            request, "invite_accept.html",
            {"request": request, "token": "", "user": None, "perms": set(),
             "year": datetime.now(timezone.utc).year, "ok": None,
             "err": "Invitation not found"},
            status_code=400,
        )
    now = _dt.now(timezone.utc)
    err = None
    if inv.revoked_at is not None:
        err = "This invitation has been revoked"
    elif inv.accepted_at is not None:
        err = "This invitation has already been used"
    elif inv.expires_at is not None and utc_aware(inv.expires_at) <= now:
        err = "This invitation has expired"
    else:
        organization = await session.get(Organization, inv.organization_id)
        if organization is None or organization.status != "ACTIVE":
            err = "This organization is not active"
    if err is not None:
        return templates.TemplateResponse(
            request, "invite_accept.html",
            {"request": request, "token": token, "user": None, "perms": set(),
             "year": datetime.now(timezone.utc).year, "ok": None, "err": err},
            status_code=400,
        )
    # reuse the API accept endpoint logic by calling the service-level flow
    from app.api.deps import get_client_ip
    from app.schemas.enterprise import InvitationAcceptIn

    payload = InvitationAcceptIn(token=token, password=password, full_name=full_name or None)

    class _Req:
        """Minimal shim exposing what the accept endpoint reads from Request."""
        app = request.app
        headers = request.headers
        base_url = request.base_url
        client = request.client

    from app.api.v1 import invitations as invitations_api

    try:
        await invitations_api.accept_invitation(
            payload, _Req(), session
        )
    except QBITError as exc:
        return templates.TemplateResponse(
            request, "invite_accept.html",
            {"request": request, "token": token, "user": None, "perms": set(),
             "year": datetime.now(timezone.utc).year, "ok": None, "err": exc.message},
            status_code=400,
        )
    return templates.TemplateResponse(
        request, "invite_accept.html",
        {"request": request, "token": "", "user": None, "perms": set(),
         "year": datetime.now(timezone.utc).year,
         "ok": "Invitation accepted — you can now sign in with your new password.",
         "err": None},
    )


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("notifications.view"))],
):
    from app.models.enterprise import Notification

    rows = (
        (await session.execute(
            select(Notification)
            .where(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc())
            .limit(100)
        )).scalars().all()
    )
    return templates.TemplateResponse(
        request, "notifications.html",
        _ctx(
            request, user,
            notifications=[
                {
                    "id": n.id,
                    "type": n.type,
                    "title": n.title,
                    "body": n.body,
                    "read": n.read_at is not None,
                    "created": str(n.created_at)[:16].replace("T", " "),
                }
                for n in rows
            ],
        ),
    )


@router.post("/notifications/{notification_id}/read")
async def notification_mark_read(
    notification_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_permission("notifications.view"))],
):
    from app.models.enterprise import Notification
    from datetime import datetime as _dt

    row = await session.get(Notification, notification_id)
    if row is not None and row.user_id == user.id and row.read_at is None:
        row.read_at = _dt.now(timezone.utc)
        await session.commit()
    return RedirectResponse(url="/notifications", status_code=303)
