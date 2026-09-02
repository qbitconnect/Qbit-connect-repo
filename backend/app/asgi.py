"""ASGI entrypoint for uvicorn.

Kept separate from `app.main` so importing the application factory never has
import-time side effects (tests and tools import `create_app` directly).

Run with:
    uvicorn app.asgi:app --host 0.0.0.0 --port 8000

Configuration comes from the environment / .env (see .env.example at repo root).
"""

from __future__ import annotations

from app.main import create_app

app = create_app()
