"""StorageService abstraction (Brief §5, §17; architecture doc 07).

- Default backend: LocalStorage rooted at the configured QBIT_DATA_DIR.
- All keys are RELATIVE to a category root; every operation is path-traversal
  guarded (core.path_safety). No raw filesystem paths are ever accepted from
  clients (Brief §22) — clients reference file ids / category-relative keys.
- Local-first: no cloud storage is required anywhere (Brief §27).
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable

from app.core.config import CATEGORY_DIRS, Settings
from app.core.errors import NotFoundError, PathAccessDeniedError, StorageError
from app.core.logging import get_logger
from app.core.path_safety import safe_filename, validate_storage_key

logger = get_logger("qbit.storage")

CHUNK_SIZE = 1024 * 1024  # 1 MiB


@dataclass(frozen=True)
class StoredObjectMeta:
    key: str
    size: int
    modified_at: datetime | None
    checksum_sha256: str | None = None


class StorageBackend:
    """Interface — future external adapters (S3/Drive) implement this."""

    name: str = "abstract"

    def save(self, key: str, data: BinaryIO | bytes, *, category: str | None = None) -> StoredObjectMeta:  # noqa: ARG002
        raise NotImplementedError

    def open(self, key: str, *, category: str | None = None) -> BinaryIO:
        raise NotImplementedError

    def delete(self, key: str, *, category: str | None = None) -> None:
        raise NotImplementedError

    def exists(self, key: str, *, category: str | None = None) -> bool:
        raise NotImplementedError

    def list(self, prefix: str = "", *, category: str | None = None) -> Iterable[str]:
        raise NotImplementedError

    def metadata(self, key: str, *, category: str | None = None) -> StoredObjectMeta:
        raise NotImplementedError

    def create_directory(self, rel_path: str, *, category: str | None = None) -> None:
        raise NotImplementedError

    def move(self, src: str, dst: str, *, category: str | None = None) -> None:
        raise NotImplementedError

    def copy(self, src: str, dst: str, *, category: str | None = None) -> None:
        raise NotImplementedError


class LocalStorage(StorageBackend):
    """Filesystem backend. Every path stays inside the configured data root."""

    name = "local"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.root = settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True)

    # -- root resolution -----------------------------------------------------
    def root_for(self, category: str | None) -> Path:
        if not category:
            return self.root
        if category == "LOG":
            return self._settings.log_dir
        return self._settings.category_dir(category)

    def _resolve(self, key: str, category: str | None) -> Path:
        try:
            return validate_storage_key(key, self.root_for(category))
        except PathAccessDeniedError:
            logger.warning("Blocked storage key access", extra={"extra_fields": {"key": key}})
            raise

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _as_bytes_io(data: BinaryIO | bytes) -> tuple[BinaryIO, int | None]:
        if isinstance(data, bytes):
            buf = io.BytesIO(data)
            return buf, len(data)
        return data, None

    @staticmethod
    def generate_key(category: str | None, filename: str, *, date: datetime | None = None) -> str:
        """Collision-free key RELATIVE to the category root: YYYY/MM/ulid_safe-name.

        (The category sub-directory itself is applied by `root_for(category)`.)
        """
        date = date or datetime.now(timezone.utc)
        safe = safe_filename(filename)
        return f"{date.strftime('%Y/%m')}/{uuid.uuid4().hex}_{safe}"

    # -- operations (contract of Brief §5) --------------------------------------
    def save(self, key: str, data: BinaryIO | bytes, *, category: str | None = None) -> StoredObjectMeta:
        target = self._resolve(key, category)
        target.parent.mkdir(parents=True, exist_ok=True)
        stream, known_size = self._as_bytes_io(data)
        hasher = hashlib.sha256()
        size = 0
        try:
            with open(target, "wb") as out:
                while chunk := stream.read(CHUNK_SIZE):
                    out.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise StorageError(f"Failed to write object: {type(exc).__name__}") from exc
        return StoredObjectMeta(
            key=key,
            size=size if known_size is None else known_size,
            modified_at=datetime.now(timezone.utc),
            checksum_sha256=hasher.hexdigest(),
        )

    def open(self, key: str, *, category: str | None = None) -> BinaryIO:
        path = self._resolve(key, category)
        if not path.is_file():
            raise NotFoundError("File not found in storage")
        try:
            return open(path, "rb")  # noqa: SIM115 - caller closes via context
        except OSError as exc:
            raise StorageError("Failed to open object") from exc

    def delete(self, key: str, *, category: str | None = None) -> None:
        path = self._resolve(key, category)
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            raise StorageError("Failed to delete object") from exc

    def exists(self, key: str, *, category: str | None = None) -> bool:
        return self._resolve(key, category).is_file()

    def list(self, prefix: str = "", *, category: str | None = None) -> Iterable[str]:
        root = self.root_for(category)
        if not root.exists():
            return []
        resolved_prefix = self._resolve(prefix, category) if prefix else root
        if resolved_prefix.is_file():
            return [str(resolved_prefix.relative_to(root))]
        return sorted(
            str(p.relative_to(root)) for p in resolved_prefix.rglob("*") if p.is_file()
        )

    def metadata(self, key: str, *, category: str | None = None) -> StoredObjectMeta:
        path = self._resolve(key, category)
        if not path.is_file():
            raise NotFoundError("File not found in storage")
        stat = path.stat()
        hasher = hashlib.sha256()
        with open(path, "rb") as fh:
            while chunk := fh.read(CHUNK_SIZE):
                hasher.update(chunk)
        return StoredObjectMeta(
            key=key,
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            checksum_sha256=hasher.hexdigest(),
        )

    def create_directory(self, rel_path: str, *, category: str | None = None) -> None:
        path = self._resolve(rel_path, category)
        path.mkdir(parents=True, exist_ok=True)

    def move(self, src: str, dst: str, *, category: str | None = None) -> None:
        src_path = self._resolve(src, category)
        dst_path = self._resolve(dst, category)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))

    def copy(self, src: str, dst: str, *, category: str | None = None) -> None:
        src_path = self._resolve(src, category)
        dst_path = self._resolve(dst, category)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)

    # -- health / stats (Brief §17, §24) ----------------------------------------
    def health(self) -> dict:
        probe_key = f"cache/healthcheck-{uuid.uuid4().hex}.tmp"
        status: dict = {
            "backend": self.name,
            "path": str(self.root),
            "directory_exists": self.root.exists(),
        }
        try:
            probe = self._resolve(probe_key, None)
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_bytes(b"ok")
            writable = probe.read_bytes() == b"ok"
            probe.unlink()
            status["writable"] = writable
            status["readable"] = self.root.exists() and os.access(self.root, os.R_OK)
        except (OSError, PathAccessDeniedError):
            status["writable"] = False
            status["readable"] = False
        try:
            usage = shutil.disk_usage(self.root)
            status["free_bytes"] = usage.free
            status["total_bytes"] = usage.total
            status["free_gb"] = round(usage.free / 1024**3, 2)
        except OSError:
            status["free_bytes"] = None
        status["status"] = (
            "online"
            if status.get("directory_exists") and status.get("writable") and status.get("readable")
            else "unavailable"
        )
        return status

    def usage_summary(self) -> dict:
        """Per-directory usage for the admin storage view (Brief §24)."""
        per_dir: dict[str, dict] = {}
        for label in ("imports", "exports", "scraper-results", "campaigns", "attachments", "misc", "backups"):
            d = self.root / label
            files = list(d.rglob("*")) if d.exists() else []
            count = sum(1 for p in files if p.is_file())
            size = sum(p.stat().st_size for p in files if p.is_file())
            per_dir[label] = {"files": count, "bytes": size}
        total_bytes = sum(v["bytes"] for v in per_dir.values())
        total_files = sum(v["files"] for v in per_dir.values())
        summary = {
            "root": str(self.root),
            "per_directory": per_dir,
            "total_files": total_files,
            "total_bytes": total_bytes,
        }
        try:
            usage = shutil.disk_usage(self.root)
            summary["disk_free_bytes"] = usage.free
            summary["disk_total_bytes"] = usage.total
            summary["disk_used_percent"] = round(usage.used / usage.total * 100, 2)
        except OSError:
            pass
        return summary


class StorageService:
    """Facade selecting the configured backend. Phase 2: local only (Brief §27)."""

    def __init__(self, settings: Settings, backend: StorageBackend | None = None) -> None:
        self.settings = settings
        self.backend: StorageBackend = backend or LocalStorage(settings)

    def __getattr__(self, item: str):  # delegate the backend contract
        return getattr(self.backend, item)

    @property
    def backend_name(self) -> str:
        return self.backend.name
