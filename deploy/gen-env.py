#!/usr/bin/env python3
"""QBIT Connect - local testing .env generator.

Stdlib-only (no dependencies). Works on Windows / macOS / Linux.

Generates a repo-root `.env` with random secrets, ready for:
    docker compose up -d --build

Usage:
    python deploy/gen-env.py            # create .env (fails if it exists)
    python deploy/gen-env.py --force    # overwrite existing .env
"""
import argparse
import secrets
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"


def token(length: int = 48) -> str:
    return secrets.token_urlsafe(length)


TEMPLATE = """\
# QBIT Connect - generated local .env ({date})
# NEVER commit this file. All secrets below were randomly generated.

QBIT_ENV=production
QBIT_SECRET_KEY={secret_key}

POSTGRES_DB=qbit
POSTGRES_USER=qbit
POSTGRES_PASSWORD={pg_password}

REDIS_PASSWORD={redis_password}

QBIT_DATA_HOST_PATH=./qbit-data
QBIT_API_PORT=8000
QBIT_CORS_ORIGINS=
QBIT_LOG_LEVEL=INFO

# --- Scraping engine (defaults are safe for local testing) ---
QBIT_SCRAPER_DISABLED_ACTORS=
QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS=false

# --- Maps provider (none = google-maps actor stays DEGRADED, by design) ---
QBIT_MAPS_PROVIDER=none
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate QBIT local .env with random secrets.")
    ap.add_argument("--force", action="store_true", help="overwrite an existing .env")
    args = ap.parse_args()

    if ENV_PATH.exists() and not args.force:
        print(f"ERROR: {ENV_PATH} already exists. Use --force to overwrite.")
        sys.exit(1)

    content = TEMPLATE.format(
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        secret_key=token(),
        pg_password=token(24),
        redis_password=token(24),
    )
    ENV_PATH.write_text(content, encoding="utf-8")

    print(f"OK   wrote {ENV_PATH}")
    print()
    print("Next steps:")
    print("  1) docker compose up -d --build")
    print("  2) docker compose exec qbit-api alembic upgrade head")
    print('  3) docker compose exec qbit-api python -m app.cli seed \\')
    print('         --email admin@example.com --password "YOUR-STRONG-PASSWORD"')
    print("  4) open http://localhost:8000")
    print()
    print("Keep this .env safe - it contains your secrets.")


if __name__ == "__main__":
    main()
