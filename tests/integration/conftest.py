"""Integration fixtures: a throwaway database on the local Postgres (docker compose up -d)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from socialcontrol.database import migrate
from socialcontrol.database.seed import seed

ADMIN_URL = os.environ.get(
    "SC_TEST_ADMIN_URL", "postgresql+psycopg://socialcontrol:socialcontrol@127.0.0.1:5433/postgres"
)


def _reachable() -> bool:
    try:
        with create_engine(ADMIN_URL, connect_args={"connect_timeout": 3}).connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def test_db_url() -> Iterator[str]:
    if not _reachable():
        pytest.skip("local Postgres not reachable (docker compose up -d)")
    name = f"sc_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'create database "{name}"'))
    url = make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as c:
            c.execute(text(f'drop database if exists "{name}" with (force)'))
        admin.dispose()


@pytest.fixture(scope="session")
def engine(test_db_url: str) -> Iterator[Engine]:
    migrate.upgrade(test_db_url)
    eng = create_engine(test_db_url, future=True)
    seed(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator:
    """A connection inside a transaction that is always rolled back."""
    connection = engine.connect()
    trans = connection.begin()
    try:
        yield connection
    finally:
        trans.rollback()
        connection.close()


DATA_TABLES = (
    "assisted_tasks, publish_attempts, post_audit, queue_slots, post_media, posts, media, "
    "queues, platform_accounts, content_items, job_runs, settings, import_batches, "
    "notification_logs, reports"
)


@pytest.fixture
def clean_db(engine: Engine) -> Iterator[Engine]:
    """Committed data is allowed in these tests; tables are truncated before and after."""
    with engine.begin() as c:
        c.execute(text(f"truncate {DATA_TABLES} cascade"))
    yield engine
    with engine.begin() as c:
        c.execute(text(f"truncate {DATA_TABLES} cascade"))
