"""Configuration system tests (Brief §3)."""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings


def test_defaults_are_local_first():
    s = Settings(_env_file=None)
    assert s.QBIT_ENV == "development"
    assert s.QBIT_DATA_DIR is not None
    # Local-first: no cloud storage anywhere in config
    assert "s3" not in str(s.DATABASE_URL).lower()
    assert "googleapis" not in str(s.DATABASE_URL).lower()


def test_category_dirs_mapping(tmp_path: Path):
    s = Settings(QBIT_DATA_DIR=tmp_path, _env_file=None)
    assert s.category_dir("EXPORT") == tmp_path / "exports"
    assert s.category_dir("IMPORT") == tmp_path / "imports"
    assert s.category_dir("SCRAPER_RESULT") == tmp_path / "scraper-results"
    assert s.category_dir("BACKUP") == tmp_path / "backups"
    assert s.category_dir("UNKNOWN_THING") == tmp_path / "misc"


def test_dir_overrides(tmp_path: Path):
    s = Settings(
        QBIT_DATA_DIR=tmp_path / "data",
        QBIT_EXPORT_DIR=tmp_path / "custom-exports",
        QBIT_BACKUP_DIR=tmp_path / "custom-backups",
        _env_file=None,
    )
    assert s.export_dir == tmp_path / "custom-exports"
    assert s.backup_dir == tmp_path / "custom-backups"
    assert s.log_dir == tmp_path / "data" / "logs"


def test_cors_parsing():
    s = Settings(QBIT_CORS_ORIGINS="https://a.example.com, https://b.example.com/", _env_file=None)
    assert s.cors_origins() == ["https://a.example.com", "https://b.example.com"]
    empty = Settings(_env_file=None)
    assert empty.cors_origins() == []


def test_production_validation_blocks_weak_secret():
    s = Settings(
        QBIT_ENV="production",
        QBIT_SECRET_KEY="dev-only-insecure-secret-key-change-me",
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/qbit",
        _env_file=None,
    )
    problems = s.validate_runtime()
    assert any("QBIT_SECRET_KEY" in p for p in problems)


def test_production_validation_blocks_sqlite_and_wildcard_cors():
    s = Settings(
        QBIT_ENV="production",
        QBIT_SECRET_KEY="x" * 48,
        DATABASE_URL="sqlite+aiosqlite:///./dev.db",
        QBIT_CORS_ORIGINS="*",
        _env_file=None,
    )
    problems = s.validate_runtime()
    assert any("PostgreSQL" in p for p in problems)
    assert any("CORS" in p for p in problems)


def test_development_with_sqlite_is_valid():
    s = Settings(QBIT_ENV="development", QBIT_SECRET_KEY="x" * 48, _env_file=None)
    assert s.validate_runtime() == []
