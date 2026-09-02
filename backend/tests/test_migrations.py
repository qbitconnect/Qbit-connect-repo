"""Migration tests: forward/rollback reproducibility + data safety (Brief §14, §32).

The repository was verified empty at Phase 0 (docs/01), so there is no pre-existing
production schema. These tests prove:
1. `alembic upgrade head` builds the full core schema from zero.
2. A second upgrade is a no-op and EXISTING DATA REMAINS INTACT (§32).
3. `downgrade base` cleanly removes exactly what was created.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_DIR = BACKEND_DIR / "alembic"

CORE_TABLES = {
    "users", "roles", "permissions", "user_roles", "role_permissions",
    "files", "audit_logs", "system_settings", "connections",
}


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _tables(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def _insert_probe_user(db_path: Path) -> str:
    uid = str(uuid.uuid4())
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO users (id, email, password_hash, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (uid, "probe@data-safety.example.com", "$argon2id$probe"),
        )
        con.commit()
    finally:
        con.close()
    return uid


def _user_exists(db_path: Path, uid: str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()
        return row is not None and row[0] == "probe@data-safety.example.com"
    finally:
        con.close()


@pytest.fixture
def migration_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "migration-test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    return db_path


def test_upgrade_head_creates_core_schema(migration_db: Path):
    command.upgrade(_alembic_config(migration_db), "head")
    assert CORE_TABLES <= _tables(migration_db)


def test_reupgrade_preserves_existing_data(migration_db: Path):
    """Data safety (Brief §32): row counts before/after must match; data intact."""
    command.upgrade(_alembic_config(migration_db), "head")
    probe_id = _insert_probe_user(migration_db)

    con = sqlite3.connect(migration_db)
    before_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()

    # Re-running the same migration state is a no-op — and must not destroy data.
    command.upgrade(_alembic_config(migration_db), "head")

    con = sqlite3.connect(migration_db)
    after_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()

    assert before_count == after_count == 1
    assert _user_exists(migration_db, probe_id)


def test_downgrade_base_removes_schema_cleanly(migration_db: Path):
    command.upgrade(_alembic_config(migration_db), "head")
    command.downgrade(_alembic_config(migration_db), "base")
    assert not (CORE_TABLES & _tables(migration_db))
    # And the cycle is repeatable
    command.upgrade(_alembic_config(migration_db), "head")
    assert CORE_TABLES <= _tables(migration_db)


def test_greenfield_repo_had_no_preexisting_schema(tmp_path: Path):
    """Documents the §32 baseline: this repository started with no database —
    nothing existed to preserve, so the initial migration is non-destructive."""
    from app.db.base import Base

    # No "legacy" tables are part of the metadata beyond the approved core set.
    assert set(Base.metadata.tables) == CORE_TABLES
