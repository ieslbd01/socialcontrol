"""ASGI entry point for hosting: ``uvicorn --factory socialcontrol.dashboard.wsgi:build``.

Builds the app from environment variables. It refuses to start without the admin login
variables when SC_ENV=prod, so a misconfigured deploy fails loudly instead of running open.
"""

from __future__ import annotations

from fastapi import FastAPI

from socialcontrol.config.settings import get_settings
from socialcontrol.dashboard.app import create_app
from socialcontrol.database.db import make_engine


def build() -> FastAPI:
    s = get_settings()
    if s.sc_env == "prod":
        missing = [
            name
            for name, value in (
                ("SC_ADMIN_EMAIL", s.sc_admin_email),
                ("SC_ADMIN_PASSWORD_HASH", s.sc_admin_password_hash),
                ("SC_SIGNING_KEY", s.sc_signing_key),
                ("DATABASE_URL", s.database_url),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"missing required settings: {', '.join(missing)}")
        if "127.0.0.1" in s.database_url or "localhost" in s.database_url:
            raise RuntimeError("DATABASE_URL points at a local database in production")
    return create_app(make_engine(), s)
