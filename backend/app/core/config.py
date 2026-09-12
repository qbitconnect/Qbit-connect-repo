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
    # --- Phase 12: authentication hardening -----------------------------------
    #: failed login attempts per account before a temporary lockout (0 = off);
    #: counters reset on success; lockout applies to BOTH api and ui logins
    QBIT_LOGIN_MAX_FAILED_ATTEMPTS: int = Field(default=10, ge=0, le=100)
    QBIT_LOGIN_LOCKOUT_MINUTES: int = Field(default=15, ge=1, le=1440)
    #: force the `secure` attribute on the UI session cookie. Default (None)
    #: means auto: secure=True in production, False elsewhere.
    QBIT_COOKIE_SECURE: bool | None = None
    # --- Phase 11: enterprise ------------------------------------------------
    QBIT_INVITATION_EXPIRY_HOURS: int = Field(default=168, ge=1, le=720)
    QBIT_RATE_LIMIT_INVITE_PER_HOUR: int = Field(default=30, ge=1)
    QBIT_RATE_LIMIT_APIKEY_PER_HOUR: int = Field(default=10, ge=1)

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

    # --- Scraping engine (Phase 3) -------------------------------------------
    QBIT_SCRAPER_ENABLED: bool = True
    #: Comma-separated actor ids to DISABLE (e.g. "google-maps,public-data").
    QBIT_SCRAPER_DISABLED_ACTORS: str = ""
    QBIT_SCRAPER_JOB_TIMEOUT_SECONDS: int = Field(default=3600, ge=10)
    QBIT_SCRAPER_REQUEST_TIMEOUT_SECONDS: int = Field(default=20, ge=1)
    QBIT_SCRAPER_MAX_RESPONSE_MB: int = Field(default=5, ge=1)
    QBIT_SCRAPER_RPS_PER_HOST: float = Field(default=1.0, gt=0)
    QBIT_SCRAPER_CONCURRENCY: int = Field(default=4, ge=1)
    QBIT_SCRAPER_MAX_RETRIES: int = Field(default=3, ge=0)
    QBIT_SCRAPER_RETRY_BASE_SECONDS: float = Field(default=2.0, ge=0.1)
    QBIT_SCRAPER_RETRY_MAX_SECONDS: float = Field(default=60.0, ge=1)
    QBIT_SCRAPER_BATCH_SIZE: int = Field(default=100, ge=1)
    QBIT_SCRAPER_CHECKPOINT_INTERVAL_SECONDS: float = Field(default=5.0, ge=1)
    QBIT_SCRAPER_PAGE_EVENT_EVERY: int = Field(default=50, ge=1)
    # --- Actor Platform (spec §26/§22) ---------------------------------------
    #: 0 disables the periodic health monitor; otherwise seconds between runs
    QBIT_ACTOR_HEALTH_INTERVAL_SECONDS: int = Field(default=300, ge=0)
    #: delivery poll cadence for run-lifecycle webhooks
    QBIT_RUN_WEBHOOK_POLL_SECONDS: float = Field(default=5.0, ge=1)
    QBIT_SCRAPER_MAX_PAGES_DEFAULT: int = Field(default=100, ge=1)
    QBIT_SCRAPER_MAX_RECORDS_DEFAULT: int = Field(default=10000, ge=1)
    #: NEVER enable in production — only for isolated test environments.
    QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS: bool = False
    QBIT_SCRAPER_ALLOWED_PORTS: str = "80,443"
    QBIT_SCRAPER_USER_AGENT: str = "QBITConnect/0.3 (+self-hosted; respectful crawler)"

    # --- Worker (Phase 3) ------------------------------------------------------
    QBIT_WORKER_LEASE_SECONDS: int = Field(default=120, ge=30)
    QBIT_WORKER_POLL_SECONDS: float = Field(default=2.0, ge=0.5)
    QBIT_WORKER_MAX_CONCURRENT_JOBS: int = Field(default=2, ge=1)
    # --- Phase 12: graceful shutdown + ops ---------------------------------------
    #: grace period before in-flight jobs are cancelled (checkpoint+PAUSED)
    #: on shutdown; must stay BELOW the container stop timeout to be effective
    QBIT_WORKER_DRAIN_SECONDS: float = Field(default=30.0, ge=1, le=600)
    #: disk usage warning threshold (percent) surfaced in /admin ops + logs
    QBIT_DISK_WARN_PERCENT: float = Field(default=85.0, ge=50.0, le=99.9)

    # --- Leads workspace (Phase 4) ---------------------------------------------
    #: rows above which an import batch is processed by the background worker
    QBIT_LEADS_INLINE_IMPORT_MAX_ROWS: int = Field(default=5000, ge=1)
    #: rows above which an export is queued for the background worker
    QBIT_LEADS_INLINE_EXPORT_MAX_ROWS: int = Field(default=20000, ge=1)
    #: max ids accepted by one bulk action call (hard delete is capped lower)
    QBIT_LEADS_MAX_BULK_IDS: int = Field(default=5000, ge=1)

    # --- Marketing engine (Phase 5) --------------------------------------------
    #: register the MOCK/TEST-ONLY marketing provider (isolated test envs only)
    QBIT_MARKETING_ALLOW_MOCK_PROVIDER: bool = False
    QBIT_MARKETING_SNAPSHOT_BATCH_SIZE: int = Field(default=1000, ge=100)
    #: queue items claimed per worker cycle
    QBIT_MARKETING_QUEUE_BATCH_SIZE: int = Field(default=25, ge=1)
    #: conservative default rate policy per sending account (operational
    #: throttling ONLY — never used to evade provider limits)
    QBIT_MARKETING_RATE_PER_MINUTE: int = Field(default=10, ge=1)
    QBIT_MARKETING_RATE_PER_HOUR: int = Field(default=100, ge=1)
    #: retry policy (brief §19): transient failures back off exponentially
    QBIT_MARKETING_MAX_ATTEMPTS: int = Field(default=3, ge=1)
    QBIT_MARKETING_RETRY_BASE_SECONDS: float = Field(default=30.0, ge=1)
    QBIT_MARKETING_RETRY_MAX_SECONDS: float = Field(default=7200.0, ge=60)
    #: audience size cap per campaign launch (hard safety ceiling)
    QBIT_MARKETING_MAX_AUDIENCE: int = Field(default=100000, ge=1)

    # --- Maps provider (Phase 3, google-maps actor) ----------------------------
    #: none | http | outscraper | mock — `mock` is for tests/dev ONLY, never production.
    QBIT_MAPS_PROVIDER: str = "none"
    QBIT_MAPS_PROVIDER_URL: str | None = None
    QBIT_MAPS_PROVIDER_API_KEY: str | None = None  # env only; never committed


    # --- WhatsApp Business provider (Phase 6 §2) --------------------------------
    #: provider registry id used when creating WhatsApp connections by default
    WHATSAPP_PROVIDER: str = "whatsapp_cloud"
    #: official Graph API base — overridable ONLY for testing/staging proxies
    WHATSAPP_API_BASE_URL: str = "https://graph.facebook.com"
    WHATSAPP_API_VERSION: str = "v21.0"
    #: webhook verification challenge token (Meta app level; never logged)
    WHATSAPP_WEBHOOK_VERIFY_TOKEN: str | None = None
    #: app secret for X-Hub-Signature-256 validation. WEBHOOK verification
    #: uses THIS app-level env secret (Meta signs at app level); per-account
    #: vault app secrets are used for SENDING credentials only, not webhook
    #: verification (corrected Phase 12 audit M9 — previous comment claimed
    #: vault precedence for webhooks, which the implementation never did).
    WHATSAPP_APP_SECRET: str | None = None
    #: dev / single-account bootstrap fallbacks — per-account ENCRYPTED vault
    #: credentials always take precedence; never returned by any API
    WHATSAPP_ACCESS_TOKEN: str | None = None
    WHATSAPP_BUSINESS_ACCOUNT_ID: str | None = None
    WHATSAPP_PHONE_NUMBER_ID: str | None = None

    # --- Email marketing provider (Phase 7 §4, §5) --------------------------------
    #: provider registry id used when creating email connections by default
    EMAIL_PROVIDER: str = "smtp"
    # SMTP (per-account vault credentials take precedence; env is a bootstrap
    # fallback for single-account deployments — never committed, never logged)
    SMTP_HOST: str | None = None
    SMTP_PORT: int | None = None
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    #: TLS | STARTTLS | NONE
    SMTP_SECURITY: str = "STARTTLS"
    # Generic transactional Email API (vendor-neutral contract, §5)
    EMAIL_API_BASE_URL: str | None = None
    EMAIL_API_KEY: str | None = None
    EMAIL_API_ACCOUNT_ID: str | None = None
    EMAIL_API_REGION: str | None = None
    #: shared secret for email provider webhook signature validation (§28)
    EMAIL_WEBHOOK_SECRET: str | None = None
    #: public base URL used to build REAL unsubscribe links (§12, §13) —
    #: no fake links are ever generated; unset means launch-time validation
    #: reports the missing configuration honestly
    QBIT_EMAIL_UNSUBSCRIBE_BASE_URL: str | None = None
    #: campaign-level tracking defaults (§31) — campaigns may override; open/
    #: click tracking is never mandatory and never claimed to be exact
    QBIT_EMAIL_DEFAULT_TRACK_OPENS: bool = False
    QBIT_EMAIL_DEFAULT_TRACK_CLICKS: bool = False
    #: append an honest unsubscribe footer when the template lacks one (§12)
    QBIT_EMAIL_APPEND_UNSUBSCRIBE_FOOTER: bool = True
    #: sender-reputation warning thresholds (§43, monitoring foundation only)
    QBIT_EMAIL_BOUNCE_WARN_RATE: float = Field(default=0.05, ge=0, le=1)
    QBIT_EMAIL_COMPLAINT_WARN_RATE: float = Field(default=0.005, ge=0, le=1)

    #: webhook replay protection: reject events older than this (seconds)
    QBIT_WEBHOOK_MAX_AGE_SECONDS: int = Field(default=600, ge=30)
    #: max inbound payload size accepted by webhook endpoints (bytes)
    QBIT_WEBHOOK_MAX_BODY_BYTES: int = Field(default=1_048_576, ge=1024)

    # --- Unified inbox (Phase 8) -------------------------------------------------
    #: when a customer replies to a RESOLVED/CLOSED conversation, reopen it
    #: (§32 business rule) instead of silently creating a new thread
    QBIT_INBOX_REOPEN_ON_REPLY: bool = True
    #: conversation visibility scope (§50): ALL | ASSIGNED_ONLY.
    #: TEAM falls back to ALL until a team model exists (audit §9.1).
    QBIT_INBOX_VISIBILITY: str = "ALL"
    #: WhatsApp customer-service window (hours) — free-text replies are allowed
    #: only inside it; outside it providers require an approved template (§22).
    #: This is the provider's published rule, shown honestly — never bypassed.
    QBIT_INBOX_WHATSAPP_WINDOW_HOURS: int = Field(default=24, ge=1, le=720)
    #: reply outbox tuning (§24): batch claimed per worker cycle + max attempts
    QBIT_INBOX_OUTBOX_BATCH_SIZE: int = Field(default=25, ge=1, le=500)
    QBIT_INBOX_OUTBOX_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=50)
    #: message page size for the conversation timeline (cursor pagination, §48)
    QBIT_INBOX_MESSAGE_PAGE_SIZE: int = Field(default=50, ge=1, le=200)

    # --- Workflow automation engine (Phase 9) -------------------------------------
    #: graph size + execution budget caps (§42) — prevent runaway definitions
    QBIT_AUTOMATION_MAX_NODES: int = Field(default=50, ge=2, le=500)
    QBIT_AUTOMATION_MAX_STEPS_PER_EXECUTION: int = Field(default=100, ge=1, le=10_000)
    #: transient-error retry policy (§38): exponential backoff between bounds
    QBIT_AUTOMATION_MAX_RETRIES: int = Field(default=3, ge=0, le=20)
    QBIT_AUTOMATION_RETRY_BASE_SECONDS: int = Field(default=30, ge=1, le=3600)
    QBIT_AUTOMATION_RETRY_MAX_SECONDS: int = Field(default=3600, ge=10, le=86400)
    #: loop protection (§40): max executions per (workflow, entity) per window
    #: and max automation-caused causation chain depth
    QBIT_AUTOMATION_MAX_EXECUTIONS_PER_WINDOW: int = Field(default=10, ge=1, le=1000)
    QBIT_AUTOMATION_WINDOW_MINUTES: int = Field(default=60, ge=1, le=1440)
    QBIT_AUTOMATION_MAX_CAUSATION_DEPTH: int = Field(default=2, ge=0, le=10)
    #: worker claim tuning (§58–§59): lease, batch size, wait cap (§28–§30)
    QBIT_AUTOMATION_LEASE_SECONDS: int = Field(default=300, ge=10, le=3600)
    QBIT_AUTOMATION_BATCH_SIZE: int = Field(default=10, ge=1, le=200)
    QBIT_AUTOMATION_MAX_WAIT_HOURS: int = Field(default=720, ge=1, le=8760)
    #: reserved-capability gates — honest defaults (§24, §54)
    QBIT_AUTOMATION_ENABLE_TEAM_ASSIGN: bool = False
    QBIT_AUTOMATION_ENABLE_START_CAMPAIGN: bool = True

    # --- Analytics & reporting engine (Phase 10) -------------------------------
    #: dashboard/metric cache TTL (Redis-backed; direct queries when unset)
    QBIT_ANALYTICS_CACHE_TTL_SECONDS: int = Field(default=60, ge=5, le=3600)
    #: aggregation refresh cadence for the background worker (seconds)
    QBIT_ANALYTICS_AGGREGATION_INTERVAL_SECONDS: int = Field(default=3600, ge=300)
    #: enable/disable the daily aggregate worker loop entirely
    QBIT_ANALYTICS_AGGREGATION_ENABLED: bool = True
    #: rows above which a report snapshot spills to a StorageService export file
    QBIT_ANALYTICS_MAX_SNAPSHOT_ROWS: int = Field(default=10_000, ge=100)

    # --- Phase 12: backup scheduling + retention (docs/21 GFS policy) --------
    #: hours between scheduled backups executed by the worker (0 = disabled);
    #: each cycle backs up the database AND the important file data dirs
    QBIT_BACKUP_SCHEDULE_HOURS: int = Field(default=24, ge=0, le=8760)
    QBIT_BACKUP_RETENTION_DAILY: int = Field(default=14, ge=1, le=365)
    QBIT_BACKUP_RETENTION_WEEKLY: int = Field(default=8, ge=1, le=104)
    QBIT_BACKUP_RETENTION_MONTHLY: int = Field(default=6, ge=1, le=60)

    # --- Derived helpers ----------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.QBIT_ENV == "production"

    @property
    def cookie_secure(self) -> bool:
        """`secure` attribute for the UI session cookie (Phase 12 audit M4).
        Auto: True in production (HTTPS assumed, brief §9), False elsewhere.
        QBIT_COOKIE_SECURE overrides explicitly for staging proxies."""
        if self.QBIT_COOKIE_SECURE is None:
            return self.is_production
        return self.QBIT_COOKIE_SECURE

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

    def scraper_allowed_ports(self) -> set[int]:
        ports: set[int] = set()
        for chunk in (self.QBIT_SCRAPER_ALLOWED_PORTS or "").split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                ports.add(int(chunk))
        return ports or {80, 443}

    def scraper_disabled_actors(self) -> set[str]:
        return {
            chunk.strip().lower()
            for chunk in (self.QBIT_SCRAPER_DISABLED_ACTORS or "").split(",")
            if chunk.strip()
        }

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
        if self.is_production and self.QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS:
            problems.append(
                "QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS must never be enabled in production (SSRF)."
            )
        if self.is_production and self.QBIT_MAPS_PROVIDER == "mock":
            problems.append(
                "QBIT_MAPS_PROVIDER=mock is forbidden in production (fake data)."
            )
        if self.is_production and not self.WHATSAPP_WEBHOOK_VERIFY_TOKEN:
            problems.append(
                "WHATSAPP_WEBHOOK_VERIFY_TOKEN must be set in production to validate "
                "WhatsApp webhook subscription requests."
            )
        if self.is_production and not self.EMAIL_WEBHOOK_SECRET:
            problems.append(
                "EMAIL_WEBHOOK_SECRET must be set in production: email provider "
                "webhooks are machine endpoints and are never processed unverified "
                "(Phase 12 audit H6)."
            )
        return problems


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor for runtime code (tests build Settings directly)."""
    return Settings()
