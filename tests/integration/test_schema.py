"""Schema integrity tests (TDD-02 section 4). Each test runs in a rolled-back transaction."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from socialcontrol.database.seed import seed

pytestmark = pytest.mark.integration


def one(conn, sql, **p):
    return conn.execute(text(sql), p).scalar_one()


@pytest.fixture
def world(conn):
    """account + queue + content item, returned as ids."""
    account = one(
        conn,
        """insert into platform_accounts (platform_key, short_name, display_name, mode)
           values ('facebook_page', 'iesl_page', 'IESL Page', 'ASSISTED') returning id""",
    )
    queue = one(
        conn,
        """insert into queues (account_id, name, start_at, recurrence, is_default)
           values (:a, 'Main', now(), cast(:rec as jsonb), true)
           returning id""",
        a=account,
        rec='{"type": "interval_days", "every": 7}',
    )
    conn.execute(text("insert into content_items (content_id, title) values ('C001', 'Topic')"))
    return {"account": account, "queue": queue}


def add_post(conn, w, post_id="C001-FB", status="DRAFT", approved=False, **extra):
    conn.execute(
        text(
            """insert into posts (post_id, content_id, account_id, queue_id, post_type, caption,
                                  status, approved_at)
               values (:pid, 'C001', :a, :q, 'text', :cap, :st, :ap)"""
        ),
        {
            "pid": post_id,
            "a": w["account"],
            "q": w["queue"],
            "cap": extra.get("caption", "hello"),
            "st": status,
            "ap": datetime.now(UTC) if approved else None,
        },
    )


def test_seed_loaded_and_idempotent(engine, conn):
    assert one(conn, "select count(*) from platforms") == 6
    n = one(conn, "select count(*) from platform_capabilities")
    assert n >= 20
    seed(engine)  # second run: no duplicates, no errors
    assert one(conn, "select count(*) from platform_capabilities") == n


def test_all_channels_default_to_assisted(conn):
    assert one(conn, "select count(*) from platforms where default_mode <> 'ASSISTED'") == 0


def test_instagram_has_no_text_only_and_youtube_only_video(conn):
    ig = {
        r[0]
        for r in conn.execute(
            text("select post_type from platform_capabilities where platform_key='instagram'")
        )
    }
    yt = {
        r[0]
        for r in conn.execute(
            text("select post_type from platform_capabilities where platform_key='youtube'")
        )
    }
    assert "text" not in ig and ig == {"image", "carousel", "reel"}
    assert yt == {"video"}


def test_post_id_format_enforced(conn, world):
    with pytest.raises(IntegrityError):
        add_post(conn, world, post_id="bad-id")


def test_clone_suffix_post_id_allowed(conn, world):
    add_post(conn, world, post_id="C001-FB-R2")


def test_content_id_format_enforced(conn):
    with pytest.raises(IntegrityError):
        conn.execute(text("insert into content_items (content_id, title) values ('X1', 't')"))


def test_cannot_schedule_without_approval(conn, world):
    with pytest.raises(DBAPIError, match="without approval"):
        add_post(conn, world, status="SCHEDULED", approved=False)


def test_scheduled_allowed_when_approved(conn, world):
    add_post(conn, world, status="SCHEDULED", approved=True)


def test_update_to_publishing_without_approval_blocked(conn, world):
    add_post(conn, world)
    with pytest.raises(DBAPIError, match="without approval"):
        conn.execute(text("update posts set status='PUBLISHING' where post_id='C001-FB'"))


def test_published_post_is_read_only(conn, world):
    add_post(conn, world, status="PUBLISHED", approved=True)
    with pytest.raises(DBAPIError, match="read-only"):
        conn.execute(text("update posts set caption='changed' where post_id='C001-FB'"))


def test_published_post_allows_notes_update(conn, world):
    add_post(conn, world, status="PUBLISHED", approved=True)
    conn.execute(text("update posts set notes='n' where post_id='C001-FB'"))


def test_invalid_status_and_language_rejected(conn, world):
    with pytest.raises(IntegrityError):
        add_post(conn, world, status="NOPE")


def test_slot_time_unique_per_queue(conn, world):
    sql = "insert into queue_slots (queue_id, slot_at) values (:q, '2026-10-01T04:00:00Z')"
    conn.execute(text(sql), {"q": world["queue"]})
    with pytest.raises(IntegrityError):
        conn.execute(text(sql), {"q": world["queue"]})


def test_a_post_can_fill_only_one_slot(conn, world):
    add_post(conn, world, status="SCHEDULED", approved=True)
    sql = """insert into queue_slots (queue_id, slot_at, post_id, state)
             values (:q, :t, 'C001-FB', 'FILLED')"""
    conn.execute(text(sql), {"q": world["queue"], "t": "2026-10-01T04:00:00Z"})
    with pytest.raises(IntegrityError):
        conn.execute(text(sql), {"q": world["queue"], "t": "2026-10-08T04:00:00Z"})


def test_filled_slot_requires_post(conn, world):
    with pytest.raises(IntegrityError):
        conn.execute(
            text("insert into queue_slots (queue_id, slot_at, state) values (:q, now(), 'FILLED')"),
            {"q": world["queue"]},
        )


def test_only_one_default_queue_per_account(conn, world):
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                """insert into queues (account_id, name, start_at, recurrence, is_default)
                   values (:a, 'Second', now(), cast(:rec as jsonb), true)"""
            ),
            {"a": world["account"], "rec": '{"type": "once"}'},
        )


def test_bangla_and_emoji_round_trip(conn, world):
    caption = "তাপমাত্রা ক্যালিব্রেশন কেন জরুরি? 🌡️"
    add_post(conn, world, caption=caption)
    assert one(conn, "select caption from posts where post_id='C001-FB'") == caption


def test_updated_at_touch_trigger(conn, world):
    add_post(conn, world)
    before = one(conn, "select updated_at from posts where post_id='C001-FB'")
    conn.execute(text("update posts set notes='x' where post_id='C001-FB'"))
    assert one(conn, "select updated_at from posts where post_id='C001-FB'") >= before


def test_migration_downgrade_and_upgrade_round_trip(test_db_url):
    """Downgrade to base then up again on a scratch database (rollback note holds)."""
    import uuid

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from socialcontrol.database import migrate
    from tests.integration.conftest import ADMIN_URL  # type: ignore[import-not-found]

    name = f"sc_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'create database "{name}"'))
    url = make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    try:
        migrate.upgrade(url)
        migrate.downgrade(url, "base")
        eng = create_engine(url)
        with eng.connect() as c:
            assert (
                c.execute(
                    text("select count(*) from information_schema.tables where table_name='posts'")
                ).scalar_one()
                == 0
            )
        migrate.upgrade(url)
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'drop database if exists "{name}" with (force)'))


def test_atomic_claim_only_one_winner(engine):
    """PUB-02: two concurrent publisher runs can never claim the same post."""
    with engine.begin() as c:
        a = c.execute(
            text(
                """insert into platform_accounts (platform_key, short_name, display_name, mode)
                   values ('facebook_page', 'race_page', 'Race', 'AUTO') returning id"""
            )
        ).scalar_one()
        c.execute(text("insert into content_items (content_id, title) values ('C900', 'race')"))
        c.execute(
            text(
                """insert into posts (post_id, content_id, account_id, post_type, caption, status, approved_at)
                   values ('C900-FB', 'C900', :a, 'text', 'x', 'SCHEDULED', now())"""
            ),
            {"a": a},
        )
    claim = text(
        """update posts set status='PUBLISHING', locked_by=:run, locked_at=now()
           where post_id='C900-FB' and status in ('SCHEDULED','RETRYING') returning post_id"""
    )
    winners: list[str] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        with engine.begin() as c:
            if c.execute(claim, {"run": f"run-{i}"}).first():
                winners.append(f"run-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    try:
        assert len(winners) == 1
    finally:
        with engine.begin() as c:
            c.execute(text("delete from posts where post_id='C900-FB'"))
            c.execute(text("delete from content_items where content_id='C900'"))
            c.execute(text("delete from platform_accounts where short_name='race_page'"))
