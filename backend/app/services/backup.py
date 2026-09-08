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
    #: data directories included in FILE backups (brief §13): important,
    #: regenerable-late but hard-to-replace data. temporary/ and cache/ are
    #: deliberately NEVER backed up (disposable runtime data).
    FILE_BACKUP_DIRS = ("exports", "scraper-results", "campaigns", "attachments", "imports")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backup_root: Path = settings.backup_dir
        self.db_dir = self.backup_root / "db"
        self.files_dir = self.backup_root / "files"
        self.config_dir = self.backup_root / "config"
        self._ensure_dirs()

    @property
    def manifest_path(self) -> Path:
        return self.backup_root / "manifest.jsonl"

    def _ensure_dirs(self) -> None:
        for d in (self.backup_root, self.db_dir, self.files_dir, self.config_dir):
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

    # ---------------------------------------------------------------- Phase 12
    def run_files_backup(self) -> BackupResult:
        """Tar.gz snapshot of the important file-data directories (§13).

        Includes exports, scraper results, campaigns, attachments and imports;
        excludes temporary/ cache/ and backups/ (never nested). The archive is
        streamed with deterministic member paths and checksummed like DB
        backups. Never fails the caller — reports honestly instead.
        """
        import tarfile

        started = datetime.now(timezone.utc)
        stamp = started.strftime("%Y%m%d-%H%M%S")
        target = self.files_dir / f"qbit-files-{stamp}.tar.gz"
        result = BackupResult(
            status="failed",
            backend="files",
            started_at=started.isoformat(),
        )
        try:
            data_root = self.settings.data_dir
            included = 0
            with tarfile.open(target, "w:gz") as tar:
                for name in self.FILE_BACKUP_DIRS:
                    directory = data_root / name
                    if not directory.exists():
                        continue
                    for member in sorted(directory.rglob("*")):
                        if member.is_file():
                            tar.add(member, arcname=str(member.relative_to(data_root)))
                            included += 1
            if included == 0:
                result.status = "completed"
                result.details["reason"] = "no file data to back up"
                target.unlink(missing_ok=True)
                return result
            result = self._finalize_file(started, target, backend="files")
            result.details["files_included"] = included
        except Exception as exc:  # noqa: BLE001 - backups report, never crash
            log_with(logger, 40, "File backup failed", error=type(exc).__name__)
            result.status = "failed"
            result.details["error"] = type(exc).__name__
            result.finished_at = datetime.now(timezone.utc).isoformat()
        self._append_manifest(result)
        return result

    def backup_config_snapshot(self) -> BackupResult:
        """Snapshot NON-SECRET configuration metadata (§12).

        Writes the .env VARIABLE NAMES required by the deployment (never
        values) plus runtime settings metadata — enough to reconstruct the
        environment shape after a host loss without ever persisting secrets.
        """
        import os

        started = datetime.now(timezone.utc)
        target = self.config_dir / f"env-manifest-{started.strftime('%Y%m%d-%H%M%S')}.txt"
        result = BackupResult(
            status="failed", backend="config", started_at=started.isoformat()
        )
        try:
            required = sorted(
                key for key in os.environ
                if key.startswith(("QBIT_", "DATABASE_URL", "REDIS_URL", "POSTGRES_", "SMTP_", "WHATSAPP_", "EMAIL_"))
            )
            lines = [f"{key}=<set>" for key in required]
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = self._finalize_file(started, target, backend="config")
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.details["error"] = type(exc).__name__
            result.finished_at = datetime.now(timezone.utc).isoformat()
        self._append_manifest(result)
        return result

    def verify_backup(self, relative_path: str) -> dict:
        """Verify a backup artifact WITHOUT touching production data (§12).

        - postgres custom dump: `pg_restore --list` parses the archive header
          and catalog (no database connection needed)
        - sqlite: SQLite integrity_check on a COPY (never the live file)
        - tar.gz files backup: full archive listing (detects truncation)
        - config snapshot: file readability
        Returns {"status": "verified"|"failed"|"unsupported", "detail": ...}.
        """
        import sqlite3 as _sqlite3
        import tarfile
        import tempfile

        path = (self.backup_root / relative_path).resolve()
        containment = self.backup_root.resolve()
        if not str(path).startswith(str(containment)):
            return {"status": "failed", "detail": "path escapes backup root"}
        if not path.exists():
            return {"status": "failed", "detail": "backup file not found"}
        try:
            if path.suffix == ".dump":
                pg_restore = shutil.which("pg_restore")
                if pg_restore is None:
                    return {"status": "unsupported", "detail": "pg_restore not found"}
                proc = subprocess.run(
                    [pg_restore, "--list", str(path)],
                    capture_output=True, text=True, timeout=600,
                )
                if proc.returncode == 0 and "TABLE DATA" in proc.stdout:
                    tables = sum(1 for line in proc.stdout.splitlines() if "TABLE DATA" in line)
                    return {"status": "verified", "detail": f"{tables} table(s) in archive"}
                return {"status": "failed", "detail": "pg_restore could not read the archive"}
            if path.suffix in {".db", ".sqlite"}:
                with tempfile.TemporaryDirectory() as tmp:
                    copy = Path(tmp) / path.name
                    shutil.copy2(path, copy)
                    conn = _sqlite3.connect(str(copy))
                    try:
                        (result_row,) = conn.execute("PRAGMA integrity_check").fetchone()
                    finally:
                        conn.close()
                    return {
                        "status": "verified" if result_row == "ok" else "failed",
                        "detail": result_row,
                    }
            if path.name.endswith((".tar.gz",)):
                with tarfile.open(path, "r:gz") as tar:
                    members = tar.getnames()
                return {"status": "verified", "detail": f"{len(members)} entries readable"}
            if path.suffix == ".txt":
                lines = path.read_text(encoding="utf-8").splitlines()
                return {"status": "verified", "detail": f"{len(lines)} variable names"}
            return {"status": "unsupported", "detail": "unknown artifact type"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "detail": type(exc).__name__}

    def read_manifest(self) -> list[dict]:
        """Full append-only manifest history (never rewritten)."""
        entries: list[dict] = []
        if self.manifest_path.exists():
            for line in self.manifest_path.read_text().splitlines():
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    def prune_backups(
        self,
        *,
        keep_daily: int,
        keep_weekly: int,
        keep_monthly: int,
    ) -> list[str]:
        """GFS retention (docs/21: 14 daily / 8 weekly / 6 monthly, §12/§29).

        ONLY artifacts recorded in the manifest are eligible — unknown files
        in the backup dirs are never touched. The newest `keep_daily` are
        always kept; within older windows the newest daily/weekly/monthly
        exemplars are retained. Returns the list of REMOVED relative paths.
        """
        now = datetime.now(timezone.utc)
        entries = [
            e for e in self.read_manifest()
            if e.get("status") == "completed" and e.get("path")
        ]
        entries.sort(key=lambda e: e.get("started_at", ""), reverse=True)

        kept: set[str] = set()
        seen_weeks: set[str] = set()
        seen_months: set[str] = set()
        daily_budget = keep_daily
        for entry in entries:
            rel = entry["path"]
            try:
                started = datetime.fromisoformat(str(entry.get("started_at")))
            except ValueError:
                kept.add(rel)  # unparseable timestamps are never pruned
                continue
            age_days = (now - started).total_seconds() / 86400
            if daily_budget > 0:
                kept.add(rel)
                daily_budget -= 1
                continue
            week = started.strftime("%G-W%V")
            month = started.strftime("%Y-%m")
            if age_days <= 7 * keep_weekly and week not in seen_weeks:
                seen_weeks.add(week)
                kept.add(rel)
                continue
            if month not in seen_months and len(seen_months) < keep_monthly:
                seen_months.add(month)
                kept.add(rel)

        removed: list[str] = []
        for entry in entries:
            rel = entry["path"]
            if rel in kept:
                continue
            candidate = (self.backup_root / rel).resolve()
            if not str(candidate).startswith(str(self.backup_root.resolve())):
                continue
            try:
                candidate.unlink()
                removed.append(rel)
                log_with(logger, 20, "Backup pruned by retention policy", path=rel)
            except OSError as exc:
                log_with(logger, 30, "Backup prune failed", path=rel, error=str(exc))
        return removed
