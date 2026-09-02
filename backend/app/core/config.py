"""Centralized configuration.

Every value is environment-driven (Brief §3, §30). Never hardcode credentials,
secret keys, storage paths or provider credentials. See `.env.example`.

Environment variable names follow the Phase 2 brief exactly:
DATABASE_URL, REDIS_URL, QBIT_DATA_DIR, QBIT_EXPORT_DIR, QBIT_LOG_DIR,
QBIT_BACKUP_DIR, QBIT_ENV, QBIT_SECRET_KEY (+ supporting QBIT_* knobs).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

QBIT_ENVS = Literal["development", "staging", "production", "test"]

#: Storage sub-directory names under QBIT_DATA_DIR (architecture doc 07).
CATEGORY_DIRS = {
    "IMPORT": "imports",
    "EXPORT": "exports",
    "SCRAPER_RESULT": "scraper-results",
    "CAMPAIGN_ATTACHMENT": "campaigns",
    "BACKUP": "backups",
    "OTHER": "misc",
}

REQUIRED_DATA_SUBDIRS = (
    "database",
    "exports",
    "imports",
    "scraper-results",
    "campaigns",
    "attachments",
    "misc",
    "logs",
    "backups",
    "temporary",
    "cache",
)

DEFAULT_SETTINGS: dict[str, str] = {
    "system.name": "QBIT Connect",
    "system.timezone": "UTC",
    "system.default_page_size": "25",
    "system.maintenance_mode": "false",
    "system.feature_flags": "{}",
    "storage.default_backend": "local",
}


class Settings(BaseSettings):
    """All runtime configuration. Values come from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Core (Brief §3) -------------------------------------------------
    QBIT_ENV: QBIT_ENVS = "development"
    QBIT_SECRET_KEY: str = "dev-only-insecure-secret-key-change-me"
    DATABASE_URL: str = "sqlite+aiosqlite:///./qbit-dev.db"
    REDIS_URL: str | None = None
    QBIT_DATA_DIR: Path = Path("./qbit-data")

    # Optional absolute overrides for individual data sub-directories.
    QBIT_EXPORT_DIR: Path | None = None
    QBIT_LOG_DIR: Path | None = None
    QBIT_BACKUP_DIR: Path | None = None

    # --- Auth / security --------------------------------------------------
    QBIT_SESSION_TTL_MINUTES: int = Field(default=480, ge=1)  # absolute token TTL
    QBIT_PASSWORD_MIN_LENGTH: int = Field(default=10, ge=8)
    QBIT_CORS_ORIGINS: str = ""  # comma-separated; empty = same-origin only
    QBIT_RATE_LIMIT_LOGIN_PER_MIN: int = Field(default=10, ge=1)
    QBIT_MAX_UPLOAD_MB: int = Field(default=100, ge=1)

    # --- API behaviour ----------------------------------------------------
    QBIT_DEFAULT_PAGE_SIZE: int = Field(default=25, ge=1, le=100)
    QBIT_MAX_PAGE_SIZE: int = Field(default=100, ge=1, le=500)

    # --- Observability / DB pool ------------------------------------------
    QBIT_LOG_LEVEL: str = "INFO"
    QBIT_DB_POOL_SIZE: int = Field(default=10, ge=1)
    QBIT_DB_MAX_OVERFLOW: int = Field(default=20, ge=0)

    # --- Seeding (CLI only; never committed) --------------------------------
    QBIT_ADMIN_EMAIL: str | None = None
    QBIT_ADMIN_PASSWORD: str | None = None
    QBIT_ADMIN_NAME: str | None = None

    # --- Derived helpers ----------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.QBIT_ENV == "production"

    @property
    def data_dir(self) -> Path:
        return self.QBIT_DATA_DIR.expanduser().resolve()

    def _resolve_override(self, override: Path | None, sub: str) -> Path:
        if override is not None:
            return override.expanduser().resolve()
        return self.data_dir / sub

    @property
    def export_dir(self) -> Path:
        return self._resolve_override(self.QBIT_EXPORT_DIR, "exports")

    @property
    def log_dir(self) -> Path:
        return self._resolve_override(self.QBIT_LOG_DIR, "logs")

    @property
    def backup_dir(self) -> Path:
        return self._resolve_override(self.QBIT_BACKUP_DIR, "backups")

    def category_dir(self, category: str) -> Path:
        """Filesystem directory for a storage category (falls back to misc)."""
        sub = CATEGORY_DIRS.get(category, "misc")
        overrides = {
            "exports": self.export_dir,
            "backups": self.backup_dir,
        }
        return overrides.get(sub, self.data_dir / sub)

    def cors_origins(self) -> list[str]:
        raw = (self.QBIT_CORS_ORIGINS or "").strip()
        if not raw:
            return []
        return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]

    def validate_runtime(self) -> list[str]:
        """Fail-fast checks. Returns list of problems (empty == OK)."""
        problems: list[str] = []
        if self.is_production and (
            not self.QBIT_SECRET_KEY
            or self.QBIT_SECRET_KEY.startswith("dev-only")
            or len(self.QBIT_SECRET_KEY) < 32
        ):
            problems.append(
                "QBIT_SECRET_KEY must be set to a strong random value (>=32 chars) in production."
            )
        if self.is_production and self.DATABASE_URL.startswith("sqlite"):
            problems.append(
                "Production requires PostgreSQL; SQLite is a development fallback only."
            )
        if self.is_production and "*" in self.cors_origins():
            problems.append("Wildcard CORS origins are forbidden in production.")
        return problems


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor for runtime code (tests build Settings directly)."""
    return Settings()
