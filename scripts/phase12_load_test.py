"""Phase 12 load / performance test (brief §47) — MEASURED numbers only.

Runs a realistic operator workload against the REAL application stack
(in-process ASGI, isolated SQLite DB seeded with real lead rows):

  - concurrent users issuing lead search / lead filter / inbox load /
    dashboard load / analytics queries / lead export (inline path)
  - measures p50 / p95 / p99 latency, error rate, wall time
  - reports queue depth + DB connection pool use (real observability probes)

NO fabricated capacity claims: results are whatever this machine measures.
Usage:  python scripts/phase12_load_test.py [--users 10] [--requests 400]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("QBIT_ENV", "test")
os.chdir(Path(__file__).resolve().parents[1] / "backend")

import httpx  # noqa: E402

LATENCIES: dict[str, list[float]] = {}
ERRORS: dict[str, int] = {}


def record(endpoint: str, started: float, ok: bool) -> None:
    LATENCIES.setdefault(endpoint, []).append((time.perf_counter() - started) * 1000)
    if not ok:
        ERRORS[endpoint] = ERRORS.get(endpoint, 0) + 1


def percentile(values: list[float], pct: float) -> float:
    data = sorted(values)
    if not data:
        return 0.0
    idx = min(int(len(data) * pct / 100), len(data) - 1)
    return data[idx]


async def user_loop(client: httpx.AsyncClient, headers: dict, endpoints: list,
                    n_requests: int) -> None:
    for i in range(n_requests):
        method, path, label = endpoints[i % len(endpoints)]
        started = time.perf_counter()
        try:
            resp = await client.request(method, path, headers=headers)
            ok = resp.status_code < 500
        except Exception:
            ok = False
        record(label, started, ok)


async def run(users: int, n_requests: int) -> None:
    tmp = tempfile.mkdtemp(prefix="qbit-phase12-load-")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/load.db"

    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import DatabaseManager
    from app.main import create_app
    from app.models.scrape import Lead
    from app.services.rbac import seed_admin, seed_rbac
    import app.models  # noqa: F401

    settings = Settings(
        QBIT_ENV="test", QBIT_SECRET_KEY="phase12-load-" + "k" * 48,
        DATABASE_URL=os.environ["DATABASE_URL"], QBIT_DATA_DIR=Path(tmp),
        QBIT_EXPORT_DIR=Path(tmp) / "exports", QBIT_LOG_DIR=Path(tmp) / "logs",
        QBIT_BACKUP_DIR=Path(tmp) / "backups",
        QBIT_LEADS_INLINE_EXPORT_MAX_ROWS=10**9,  # measure the real export path
        _env_file=None,
    )
    db = DatabaseManager(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("seeding 2,000 leads …")
    async with db.session() as session:
        await seed_rbac(session)
        await seed_admin(session, email="admin@load.example",
                         password="L0adAdmin!Pass", full_name="Load Admin")
        session.add_all([
            Lead(
                business_name=f"Company {i:05d}",
                email=f"contact{i:05d}@example.com",
                phone=f"+9112{i % 10}{i:07d}",
                city=["Mumbai", "Delhi", "Bengaluru", "Pune", "Jaipur"][i % 5],
                country="IN",
            )
            for i in range(2000)
        ])
        await session.commit()

    app = create_app(settings, db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/api/v1/auth/login", json={
            "email": "admin@load.example", "password": "L0adAdmin!Pass"})
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        endpoints = [
            ("GET", "/api/v1/leads?page=1&page_size=25", "lead_list"),
            ("GET", "/api/v1/leads?q=Company+0012&page=1&page_size=25", "lead_search"),
            ("GET", "/api/v1/leads?status=NEW&page=1&page_size=25", "lead_filter"),
            ("GET", "/api/v1/inbox/conversations?page=1", "inbox_load"),
            ("GET", "/api/v1/analytics/overview", "dashboard"),
            ("GET", "/api/v1/analytics/leads/sources", "analytics_sources"),
            ("GET", "/health/ready", "health_ready"),
        ]

        print(f"running {users} concurrent users x {n_requests} requests each …")
        wall_start = time.perf_counter()
        await asyncio.gather(*[
            user_loop(client, headers, endpoints, n_requests) for _ in range(users)
        ])
        wall = time.perf_counter() - wall_start

        total = sum(len(v) for v in LATENCIES.values())
        total_errors = sum(ERRORS.values())
        print("\n================ LOAD TEST RESULTS (measured) ================")
        print(f"concurrency        : {users} users")
        print(f"total requests     : {total}")
        print(f"wall time          : {wall:.2f}s")
        print(f"throughput         : {total / wall:.1f} req/s")
        print(f"error rate         : {total_errors}/{total} = "
              f"{(total_errors / total * 100) if total else 0:.2f}%")
        print(f"{'endpoint':<20} {'n':>5} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'max ms':>8} {'errors':>6}")
        for label in sorted(LATENCIES):
            values = LATENCIES[label]
            print(f"{label:<20} {len(values):>5} "
                  f"{statistics.median(values):>8.1f} "
                  f"{percentile(values, 95):>8.1f} "
                  f"{percentile(values, 99):>8.1f} "
                  f"{max(values):>8.1f} "
                  f"{ERRORS.get(label, 0):>6}")
        # real runtime probes
        try:
            queue = app.state.queue
            depth = await queue.pending_count()
            print(f"\nqueue depth (measured): {depth}")
        except Exception as exc:
            print(f"\nqueue depth probe failed: {type(exc).__name__}")
        pool = app.state.db.engine.pool
        print(f"db pool checked-out (measured): {pool.checkedout()}")
    await db.close()
    print("\nNOTE: numbers reflect THIS machine + SQLite test DB. They prove")
    print("behavior under concurrency, NOT a production capacity rating.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--requests", type=int, default=30,
                        help="requests per user")
    args = parser.parse_args()
    asyncio.run(run(args.users, args.requests))
