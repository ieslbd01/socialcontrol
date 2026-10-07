"""Database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from socialcontrol.config.settings import get_settings


def make_engine(url: str | None = None) -> Engine:
    """Engine for local Postgres or Supabase.

    Supabase's pooler (``*.pooler.supabase.com``) is PgBouncer: prepared statements are turned off
    there. Use the **Session pooler** string from the Supabase dashboard; the direct connection is
    IPv6-only on the free plan and does not work from GitHub Actions.
    """
    url = url or get_settings().database_url
    args: dict[str, object] = {}
    if "pooler.supabase" in url:
        args["prepare_threshold"] = None
    return create_engine(url, pool_pre_ping=True, future=True, connect_args=args)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
