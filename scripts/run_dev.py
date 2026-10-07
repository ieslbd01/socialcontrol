"""Run the dashboard locally for development.

Uses SC_ADMIN_EMAIL / SC_ADMIN_PASSWORD_HASH from .env. If they are not set and SC_ENV=local,
a throw-away dev login is created and printed. Never use this script in production.
"""

from __future__ import annotations

import sys

import uvicorn

from socialcontrol.config.settings import get_settings
from socialcontrol.dashboard import auth
from socialcontrol.dashboard.app import create_app
from socialcontrol.database.db import make_engine

DEV_EMAIL = "dev@localhost.test"
DEV_PASSWORD = "dev-password-123"


def main() -> int:
    s = get_settings()
    if s.sc_env != "local":
        print("run_dev.py is for SC_ENV=local only")
        return 2
    updates: dict[str, str] = {}
    if not (s.sc_admin_email and s.sc_admin_password_hash):
        updates["sc_admin_email"] = DEV_EMAIL
        updates["sc_admin_password_hash"] = auth.hash_password(DEV_PASSWORD)
        print(f"\n  Dev login  ->  {DEV_EMAIL}  /  {DEV_PASSWORD}\n")
    if len(s.sc_signing_key) < 16:
        updates["sc_signing_key"] = "dev-only-signing-key-change-me-0123456789"
    s = s.model_copy(update=updates)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    uvicorn.run(create_app(make_engine(), s), host="127.0.0.1", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
