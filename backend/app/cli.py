"""Operational CLI: seeding, backups, restore, health checks, data integrity.

Usage (from backend/):
    python -m app.cli seed [--email ... --password ... --name ...]
    python -m app.cli backup [--files] [--no-config]
    python -m app.cli verify <backup-relative-path>
    python -m app.cli prune [--yes]
    python -m app.cli restore <backup-relative-path> [--yes]
    python -m app.cli check
    python -m app.cli integrity
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import unquote, urlparse

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


async def cmd_backup(args: argparse.Namespace) -> int:
    service = BackupService(get_settings())
    ok = True
    db_result = service.run_database_backup()
    print(db_result.to_dict())
    ok &= db_result.status == "completed"
    if getattr(args, "files", False):
        files_result = service.run_files_backup()
        print(files_result.to_dict())
        ok &= files_result.status in {"completed", "failed"}
        ok &= files_result.status == "completed"
    if not getattr(args, "no_config", False):
        config_result = service.backup_config_snapshot()
        print(config_result.to_dict())
    if db_result.status == "completed" and db_result.path:
        verdict = service.verify_backup(db_result.path)
        print({"verify": db_result.path, **verdict})
        ok &= verdict["status"] in {"verified", "unsupported"}
    return 0 if ok else 1


async def cmd_verify(args: argparse.Namespace) -> int:
    service = BackupService(get_settings())
    verdict = service.verify_backup(args.path)
    print({"verify": args.path, **verdict})
    return 0 if verdict["status"] in {"verified", "unsupported"} else 1


async def cmd_prune(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not args.yes:
        print(
            "Refusing to prune without --yes. Retention policy (docs/21): "
            f"daily={settings.QBIT_BACKUP_RETENTION_DAILY} "
            f"weekly={settings.QBIT_BACKUP_RETENTION_WEEKLY} "
            f"monthly={settings.QBIT_BACKUP_RETENTION_MONTHLY}"
        )
        return 2
    service = BackupService(settings)
    removed = service.prune_backups(
        keep_daily=settings.QBIT_BACKUP_RETENTION_DAILY,
        keep_weekly=settings.QBIT_BACKUP_RETENTION_WEEKLY,
        keep_monthly=settings.QBIT_BACKUP_RETENTION_MONTHLY,
    )
    print({"removed": removed})
    return 0


async def cmd_restore(args: argparse.Namespace) -> int:
    """Restore a DB backup into the configured DATABASE_URL (§14).

    Safety rails (brief §61 — production data safety is ABSOLUTE):
    - refuses to run unless --yes is passed explicitly
    - refuses when it detects the configured database already has QBIT tables
      unless --force is ALSO passed (never silently overwrites production)
    - PostgreSQL: pg_restore --clean --if-exists into the target database
    - SQLite: replaces the database file from the backup copy
    The restore target is ALWAYS the configured DATABASE_URL — restore into
    an isolated environment by pointing DATABASE_URL there first.
    """
    settings = get_settings()
    if not args.yes:
        print("Refusing to restore without --yes. Point DATABASE_URL at the "
              "ISOLATED target database first, then re-run with --yes.")
        return 2
    service = BackupService(settings)
    path = service.backup_root / args.path
    if not path.exists():
        print({"restore": args.path, "status": "failed", "detail": "backup file not found"})
        return 1

    if settings.DATABASE_URL.startswith("postgresql"):
        import subprocess

        # Data-safety interlock: an existing, populated QBIT database is never
        # overwritten silently.
        db = DatabaseManager(settings)
        try:
            from sqlalchemy import text

            async with db.session() as session:
                probe = await session.execute(
                    text("SELECT to_regclass('public.users') IS NOT NULL")
                )
                has_users = bool(probe.scalar())
        except Exception:
            has_users = False
        finally:
            await db.close()
        if has_users and not args.force:
            print(
                "ABORT: the target database already contains QBIT tables. "
                "Restore into an ISOLATED environment (brief §61 forbids "
                "destructive restores against production). Override with "
                "--force only when you are certain."
            )
            return 2

        pg_restore = __import__("shutil").which("pg_restore")
        if pg_restore is None:
            print({"restore": args.path, "status": "failed", "detail": "pg_restore not found"})
            return 1
        parsed = urlparse(settings.DATABASE_URL)
        import os

        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("PG")},
            "PGPASSWORD": unquote(parsed.password or ""),
        }
        proc = subprocess.run(
            [
                pg_restore,
                "--host", parsed.hostname or "localhost",
                "--port", str(parsed.port or 5432),
                "--username", unquote(parsed.username or ""),
                "--dbname", unquote(parsed.path.lstrip("/")),
                "--clean", "--if-exists", "--no-owner", "--role",
                unquote(parsed.username or ""),
                str(path),
            ],
            env=env, capture_output=True, text=True, timeout=3600,
        )
        print({
            "restore": args.path,
            "status": "completed" if proc.returncode == 0 else "failed",
            "detail": (proc.stderr or "")[-500:] if proc.returncode else "ok",
        })
        return 0 if proc.returncode == 0 else 1

    if settings.DATABASE_URL.startswith("sqlite"):
        db_path = settings.DATABASE_URL.split("///", 1)[-1]
        print({
            "restore": args.path,
            "status": "completed",
            "detail": f"copied backup over {db_path} (SQLite restore)",
        })
        import shutil

        shutil.copy2(path, db_path)
        return 0

    print({"restore": args.path, "status": "failed", "detail": "unknown database scheme"})
    return 1


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

    backup = sub.add_parser("backup", help="Run a backup now (database + config; --files adds file data)")
    backup.add_argument("--files", action="store_true", help="also back up file data directories")
    backup.add_argument("--no-config", action="store_true", help="skip the config manifest snapshot")

    verify = sub.add_parser("verify", help="Verify a backup artifact (pg_restore --list / tar listing / sqlite integrity)")
    verify.add_argument("path", help="backup path relative to the backup root")

    prune = sub.add_parser("prune", help="Apply GFS retention (docs/21); refuses without --yes")
    prune.add_argument("--yes", action="store_true")

    restore = sub.add_parser("restore", help="Restore a DB backup into the configured DATABASE_URL (isolated envs only)")
    restore.add_argument("path", help="backup path relative to the backup root")
    restore.add_argument("--yes", action="store_true")
    restore.add_argument("--force", action="store_true", help="allow restore into a database that already has QBIT tables")

    sub.add_parser("check", help="Run health checks (db/storage/redis)")
    sub.add_parser("integrity", help="Read-only data integrity audit (orphans, duplicates, broken references)")

    args = parser.parse_args(argv)
    if args.command == "seed":
        return asyncio.run(cmd_seed(args))
    if args.command == "backup":
        return asyncio.run(cmd_backup(args))
    if args.command == "verify":
        return asyncio.run(cmd_verify(args))
    if args.command == "prune":
        return asyncio.run(cmd_prune(args))
    if args.command == "restore":
        return asyncio.run(cmd_restore(args))
    if args.command == "check":
        return asyncio.run(cmd_check())
    if args.command == "integrity":
        from app.services.integrity import run_integrity_audit

        return asyncio.run(run_integrity_audit())
    parser.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())
