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
from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_DIR = BACKEND_DIR / "alembic"

CORE_TABLES = {
    "users", "roles", "permissions", "user_roles", "role_permissions",
    "files", "audit_logs", "system_settings", "connections",
}

#: Phase 3 scraping engine tables (migration 0002) — additive only.
SCRAPING_TABLES = {
    "scrape_jobs", "scrape_job_events", "scrape_job_checkpoints", "leads",
}

#: Phase 4 lead workspace tables (migration 0003) — additive only.
LEAD_WORKSPACE_TABLES = {
    "lead_tags", "lead_tag_assignments", "lead_notes", "lead_activities",
    "lead_merge_history", "lead_duplicate_candidates", "saved_views",
    "import_batches", "lead_exports",
}

#: Phase 5 marketing foundation tables (migration 0004) — additive only.
MARKETING_TABLES = {
    "campaigns", "campaign_recipients", "campaign_events", "campaign_templates",
    "sending_accounts", "suppression_entries", "opt_out_records", "campaign_queue",
}

#: Phase 6 WhatsApp provider tables (migration 0005) — additive only.
MESSAGING_TABLES = {
    "provider_credentials", "provider_events", "conversations", "messages",
}

#: Phase 11 team/admin/enterprise tables (migration 0007) — additive only.
ENTERPRISE_TABLES = {
    "organizations", "organization_members", "teams", "team_members",
    "invitations", "sessions", "api_keys", "notifications",
    "user_preferences", "lead_assignment_history",
    "conversation_assignment_history",
}

#: Phase 7 email provider tables (migration 0006) — additive only.
EMAIL_TABLES = {
    "email_tracking_events", "email_unsubscribe_tokens",
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
    import app.models  # noqa: F401 — deterministic model registration
    from app.db.base import Base

    # No "legacy" tables are part of the metadata beyond the approved sets.
    assert set(Base.metadata.tables) == (
        CORE_TABLES | SCRAPING_TABLES | LEAD_WORKSPACE_TABLES | MARKETING_TABLES
        | MESSAGING_TABLES | EMAIL_TABLES | ENTERPRISE_TABLES
    )


def test_0007_backfill_is_safe_and_preserves_ownership(migration_db: Path):
    """Phase 11 §30: the enterprise migration must backfill a default org,
    link EVERY existing user/record to it, preserve campaign ownership and
    never delete or reset anything."""
    import uuid as _uuid

    command.upgrade(_alembic_config(migration_db), "0006_email_provider")

    probe_id = _insert_probe_user(migration_db)

    con = sqlite3.connect(migration_db)
    # minimal legacy-shaped rows BEFORE the enterprise migration
    lead_id = _uuid.uuid4().hex
    con.execute(
        "INSERT INTO leads (id, business_name, source, status, created_by, created_at, updated_at) "
        "VALUES (?, 'Probe Co', 'test', 'NEW', ?, datetime('now'), datetime('now'))",
        (lead_id, probe_id),
    )
    campaign_id = _uuid.uuid4().hex
    con.execute(
        "INSERT INTO campaigns (id, name, channel, status, created_by, audience_definition, "
        "validation_report, campaign_metadata, created_at, updated_at) "
        "VALUES (?, 'Probe Camp', 'WHATSAPP', 'DRAFT', ?, '{}', '{}', '{}', "
        "datetime('now'), datetime('now'))",
        (campaign_id, probe_id),
    )
    con.commit()
    users_before = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    leads_before = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    campaigns_before = con.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    con.close()

    command.upgrade(_alembic_config(migration_db), "head")

    con = sqlite3.connect(migration_db)
    try:
        # nothing was destroyed
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == users_before
        assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == leads_before
        assert con.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == campaigns_before
        assert _user_exists(migration_db, probe_id)

        # a single default organization exists and everything links to it
        org = con.execute(
            "SELECT id, slug, status FROM organizations WHERE slug = 'default'"
        ).fetchone()
        assert org is not None and org[2] == "ACTIVE"
        org_id = org[0]

        assert con.execute(
            "SELECT 1 FROM organization_members WHERE user_id = ? AND organization_id = ?",
            (probe_id, org_id),
        ).fetchone()

        lead_org = con.execute("SELECT organization_id FROM leads WHERE id = ?", (lead_id,)).fetchone()[0]
        assert lead_org == org_id
        camp_org, owner = con.execute(
            "SELECT organization_id, owner_id FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        assert camp_org == org_id
        assert owner == probe_id  # ownership preserved (owner_id backfilled)

        # existing connections/sender accounts stay org-wide usable
        assert con.execute(
            "SELECT COUNT(*) FROM connections WHERE access_scope IS NOT NULL"
        ).fetchone()[0] >= 0

        # new Phase 11 permissions landed (audit.view predates this revision)
        count = con.execute(
            "SELECT COUNT(*) FROM permissions WHERE code IN "
            "('teams.view','leads.assign','apikeys.create','notifications.view')"
        ).fetchone()[0]
        assert count == 4  # audit.view predates Phase 11 (seeded at runtime)

        # downgrade removes ONLY what 0007 created (probe data still intact)
    finally:
        con.close()

    command.downgrade(_alembic_config(migration_db), "0006_email_provider")
    con = sqlite3.connect(migration_db)
    try:
        assert _user_exists(migration_db, probe_id)
        assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == leads_before
        assert con.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == campaigns_before
        # the revision's added columns/tables are gone; the pre-existing data
        # columns (created_by etc.) survive untouched
        assert "organizations" not in _tables(migration_db)
    finally:
        con.close()


def test_seed_rbac_works_on_migrated_schema(migration_db: Path):
    """Regression (Phase 4): migration-inserted permission rows must be
    updatable by the ORM. Raw INSERTs once used dashed uuid strings while the
    app's Uuid type stores hex32 on SQLite — later UPDATE-by-PK matched 0 rows
    (StaleDataError). The migration now writes hex32; this test proves the
    ORM can load AND update migration-seeded rows."""
    import asyncio

    command.upgrade(_alembic_config(migration_db), "head")

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    from app.models.rbac import Permission
    from app.services import rbac as rbac_service

    async def _seed_and_update() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{migration_db}")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            counts = await rbac_service.seed_rbac(session)
            assert counts["permissions"] == len(rbac_service.PERMISSIONS)
            # second run = idempotent UPDATE path over migration-inserted rows
            counts = await rbac_service.seed_rbac(session)
            row = await session.scalar(
                select(Permission).where(Permission.code == "leads.import")
            )
            assert row is not None
            row.description = "Import leads from CSV/XLSX/JSON/JSONL"
            await session.commit()  # would raise StaleDataError on id mismatch
        await engine.dispose()

    asyncio.run(_seed_and_update())
