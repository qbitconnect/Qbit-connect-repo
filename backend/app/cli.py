"""Operational CLI: seeding, backups, health checks.

Usage (from backend/):
    python -m app.cli seed [--email ... --password ... --name ...]
    python -m app.cli backup
    python -m app.cli check
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import DatabaseManager
from app.services.backup import BackupService
from app.services.health import HealthService
from app.services.storage import StorageService
from app.redis_client import RedisManager

logger = get_logger("qbit.cli")


async def cmd_seed(args: argparse.Namespace) -> int:
    from app.core.config import Settings
    from app.core.security import generate_password
    from app.services import rbac as rbac_service

    settings: Settings = get_settings()
    db = DatabaseManager(settings)
    try:
        async with db.session() as session:
            counts = await rbac_service.seed_rbac(session)
            print(f"Seeded roles/permissions: {counts}")

            email = args.email or settings.QBIT_ADMIN_EMAIL
            password = args.password or settings.QBIT_ADMIN_PASSWORD
            if not email or not password:
                print("No admin credentials provided (QBIT_ADMIN_EMAIL / QBIT_ADMIN_PASSWORD); "
                      "seeded roles only.")
                return 0
            user_id, created = await rbac_service.seed_admin(
                session, email=email, password=password, full_name=args.name
            )
            if created:
                print(f"Created SUPER_ADMIN {email} (id={user_id})")
            else:
                print(f"SUPER_ADMIN {email} already exists (id={user_id}) — left untouched")
        return 0
    finally:
        await db.close()


async def cmd_backup() -> int:
    settings = get_settings()
    result = BackupService(settings).run_database_backup()
    print(result.to_dict())
    return 0 if result.status == "completed" else 1


async def cmd_check() -> int:
    settings = get_settings()
    db = DatabaseManager(settings)
    storage = StorageService(settings)
    redis = RedisManager(settings)
    health = HealthService(db, storage, redis)
    try:
        overall, code = await health.overall()
        print(overall)
        return 0 if code == 200 else 1
    finally:
        await db.close()
        await redis.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qbit", description="QBIT Connect operations CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Seed roles/permissions and (optionally) the first SUPER_ADMIN")
    seed.add_argument("--email", default=None)
    seed.add_argument("--password", default=None)
    seed.add_argument("--name", default=None)

    sub.add_parser("backup", help="Run a database backup now (foundation hook)")
    sub.add_parser("check", help="Run health checks (db/storage/redis)")

    args = parser.parse_args(argv)
    if args.command == "seed":
        return asyncio.run(cmd_seed(args))
    if args.command == "backup":
        return asyncio.run(cmd_backup())
    if args.command == "check":
        return asyncio.run(cmd_check())
    parser.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())
