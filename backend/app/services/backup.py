"""BackupService foundation (Brief §23).

Phase 2 scope: configuration + directory structure + database backup hooks.
- No scheduling yet (later phase).
- NEVER deletes backups automatically (Brief §26).
- pg_dump is invoked without credentials on the command line (PGPASSWORD env).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote

from app.core.config import Settings
from app.core.logging import get_logger, log_with

logger = get_logger("qbit.backup")


@dataclass
class BackupResult:
    status: str  # completed | unsupported | failed
    backend: str
    started_at: str
    finished_at: str | None = None
    path: str | None = None
    size_bytes: int | None = None
    checksum_sha256: str | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "backend": self.backend,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "checksum_sha256": self.checksum_sha256,
            "details": self.details,
        }


class BackupService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backup_root: Path = settings.backup_dir
        self.db_dir = self.backup_root / "db"
        self.config_dir = self.backup_root / "config"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for d in (self.backup_root, self.db_dir, self.config_dir):
            d.mkdir(parents=True, exist_ok=True)

    def run_database_backup(self) -> BackupResult:
        started = datetime.now(timezone.utc)
        result = BackupResult(
            status="unsupported",
            backend=self._backend_kind(),
            started_at=started.isoformat(),
        )
        url = self.settings.DATABASE_URL
        try:
            if url.startswith("postgresql"):
                result = self._backup_postgres(started)
            elif url.startswith("sqlite"):
                result = self._backup_sqlite_file(started)
            else:
                result.details["reason"] = "unknown database scheme"
        except Exception as exc:  # noqa: BLE001 - backups report, never crash the app
            log_with(logger, 40, "Backup failed", error=str(exc))
            result.status = "failed"
            result.details["error"] = type(exc).__name__
            result.finished_at = datetime.now(timezone.utc).isoformat()
        self._append_manifest(result)
        return result

    def _backend_kind(self) -> str:
        url = self.settings.DATABASE_URL
        if url.startswith("postgresql"):
            return "postgresql"
        if url.startswith("sqlite"):
            return "sqlite"
        return "unknown"

    def _backup_postgres(self, started: datetime) -> BackupResult:
        pg_dump = shutil.which("pg_dump")
        if pg_dump is None:
            return BackupResult(
                status="unsupported",
                backend="postgresql",
                started_at=started.isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat(),
                details={"reason": "pg_dump binary not found on host"},
            )
        parsed = urlparse(self.settings.DATABASE_URL)
        stamp = started.strftime("%Y%m%d-%H%M%S")
        target = self.db_dir / f"qbit-db-{stamp}.dump"
        env: dict[str, str] = {
            **{k: v for k, v in __import__("os").environ.items() if not k.startswith("PG")},
            "PGPASSWORD": unquote(parsed.password or ""),
        }
        cmd = [
            pg_dump,
            "--host", parsed.hostname or "localhost",
            "--port", str(parsed.port or 5432),
            "--username", unquote(parsed.username or ""),
            "--format", "custom",
            "--file", str(target),
            unquote(parsed.path.lstrip("/")),
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            return BackupResult(
                status="failed",
                backend="postgresql",
                started_at=started.isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat(),
                details={"reason": "pg_dump returned non-zero exit"},
            )
        return self._finalize_file(started, target, backend="postgresql")

    def _backup_sqlite_file(self, started: datetime) -> BackupResult:
        path_part = self.settings.DATABASE_URL.split("///", 1)[-1]
        source = Path(path_part)
        if not source.exists():
            return BackupResult(
                status="failed",
                backend="sqlite",
                started_at=started.isoformat(),
                details={"reason": "sqlite database file not found"},
            )
        stamp = started.strftime("%Y%m%d-%H%M%S")
        target = self.db_dir / f"qbit-db-{stamp}.sqlite"
        shutil.copy2(source, target)
        return self._finalize_file(started, target, backend="sqlite")

    def _finalize_file(self, started: datetime, target: Path, *, backend: str) -> BackupResult:
        hasher = hashlib.sha256()
        with open(target, "rb") as fh:
            while chunk := fh.read(1024 * 1024):
                hasher.update(chunk)
        finished = datetime.now(timezone.utc)
        log_with(
            logger, 20, "Backup completed", backend=backend,
            size=target.stat().st_size,
        )
        return BackupResult(
            status="completed",
            backend=backend,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            path=str(target.relative_to(self.backup_root)),
            size_bytes=target.stat().st_size,
            checksum_sha256=hasher.hexdigest(),
        )

    def _append_manifest(self, result: BackupResult) -> None:
        """Append-only manifest — history is never rewritten or pruned here."""
        manifest = self.backup_root / "manifest.jsonl"
        try:
            with open(manifest, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(result.to_dict(), default=str) + "\n")
        except OSError as exc:  # pragma: no cover
            log_with(logger, 40, "Could not write backup manifest", error=str(exc))
