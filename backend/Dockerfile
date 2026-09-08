# QBIT Connect API — production-oriented image (Brief §29)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/qbit

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 qbit

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN chown -R qbit:qbit /opt/qbit
USER qbit

EXPOSE 8000

# Migrations run as a separate, controlled step (never automatic on boot):
#   docker compose exec qbit-api alembic upgrade head
# Phase 12 (audit M2): HEALTHCHECK uses /health/live (LIVENESS — zero
# dependency probes) so a transient DB/Redis blip no longer marks the API
# container unhealthy and eligible for restart. Readiness is served by
# /health/ready for orchestrators/load balancers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status==200 else 1)" \
        || exit 1

CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
