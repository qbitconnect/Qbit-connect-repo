"""StorageService tests (Brief §5, §17, §27)."""

from __future__ import annotations

import io

import pytest

from app.core.config import Settings
from app.core.errors import NotFoundError, PathAccessDeniedError
from app.services.storage import LocalStorage, StorageService


@pytest.fixture
def storage(tmp_path) -> StorageService:
    settings = Settings(
        QBIT_DATA_DIR=tmp_path / "data",
        QBIT_EXPORT_DIR=tmp_path / "data" / "exports",
        _env_file=None,
    )
    return StorageService(settings)


def test_save_read_roundtrip(storage: StorageService):
    meta = storage.save("exports/2026/09/a.txt", b"hello qbit", category=None)
    assert meta.size == 10
    assert meta.checksum_sha256
    with storage.open("exports/2026/09/a.txt") as fh:
        assert fh.read() == b"hello qbit"
    assert storage.exists("exports/2026/09/a.txt")


def test_streamed_save_matches_checksum(storage: StorageService):
    data = b"streamed-content-12345" * 100
    meta = storage.save("exports/x.bin", io.BytesIO(data), category=None)
    assert meta.size == len(data)
    assert meta.checksum_sha256
    from app.core.config import Settings

    s: Settings = storage.settings
    file_path = s.data_dir / "exports" / "x.bin"
    assert file_path.read_bytes() == data


def test_category_root_isolation(storage: StorageService):
    key = storage.generate_key("EXPORT", "report.csv")
    storage.save(key, b"export-data", category="EXPORT")
    # Key is relative to the EXPORT root (exports/).
    assert storage.exists(key, category="EXPORT")
    assert not storage.exists(key, category="IMPORT")
    listed = list(storage.list(category="EXPORT"))
    assert key in listed


def test_delete_and_exists(storage: StorageService):
    storage.save("misc/f.txt", b"x", category=None)
    assert storage.exists("misc/f.txt", category=None)
    storage.delete("misc/f.txt", category=None)
    assert not storage.exists("misc/f.txt", category=None)


def test_delete_missing_is_noop(storage: StorageService):
    storage.delete("misc/never-existed.txt", category=None)  # must not raise


def test_open_missing_raises_not_found(storage: StorageService):
    with pytest.raises(NotFoundError):
        storage.open("misc/nope.txt", category=None)


def test_metadata_includes_checksum(storage: StorageService):
    storage.save("misc/m.txt", b"meta", category=None)
    meta = storage.metadata("misc/m.txt", category=None)
    import hashlib

    assert meta.checksum_sha256 == hashlib.sha256(b"meta").hexdigest()


def test_move_and_copy(storage: StorageService):
    storage.save("misc/src.txt", b"payload", category=None)
    storage.copy("misc/src.txt", "misc/copy.txt", category=None)
    storage.move("misc/copy.txt", "misc/moved.txt", category=None)
    assert storage.exists("misc/src.txt", category=None)
    assert not storage.exists("misc/copy.txt", category=None)
    assert storage.exists("misc/moved.txt", category=None)


def test_create_directory_and_list(storage: StorageService):
    storage.create_directory("misc/nested/dir", category=None)
    storage.save("misc/nested/dir/f1.txt", b"1", category=None)
    storage.save("misc/nested/dir/f2.txt", b"2", category=None)
    keys = list(storage.list("misc/nested", category=None))
    assert "misc/nested/dir/f1.txt" in keys and "misc/nested/dir/f2.txt" in keys


def test_list_prefix_traversal_blocked(storage: StorageService):
    with pytest.raises(PathAccessDeniedError):
        storage.list("../", category=None)


def test_health_reports_online(storage: StorageService):
    health = storage.backend.health()
    assert health["status"] == "online"
    assert health["writable"] is True
    assert health["readable"] is True
    assert health["free_gb"] is not None
    # No credentials or secrets in health output
    assert "password" not in str(health).lower()


def test_usage_summary_counts(storage: StorageService):
    storage.save("exports/one.csv", b"1,2,3", category=None)
    storage.save("misc/two.txt", b"hello", category=None)
    summary = storage.backend.usage_summary()
    assert summary["total_files"] >= 2
    assert summary["per_directory"]["exports"]["files"] >= 1
    assert "disk_free_bytes" in summary


def test_storage_contract_via_facade(storage: StorageService):
    """The StorageService facade exposes the full backend contract (Brief §5)."""
    for op in ("save", "open", "delete", "exists", "list", "metadata", "create_directory", "move", "copy"):
        assert callable(getattr(storage, op)), op


def test_local_backend_name():
    assert LocalStorage.name == "local"
