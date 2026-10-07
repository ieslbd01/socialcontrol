"""Publisher engine tests: PRD-07 / TST-01 T-PUB-*, T-AST-01, T-REV-01/02/04. Mock adapter, fake clock."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from socialcontrol.domain.workflow import approval_hash
from socialcontrol.platforms.adapters.mock import MockAdapter
from socialcontrol.publisher.engine import run_once

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)  # slot time = 10:00 Dhaka
DATA_TABLES = (
    "assisted_tasks, publish_attempts, post_audit, queue_slots, post_media, posts, media, "
    "queues, platform_accounts, content_items, job_runs, settings, import_batches"
)


@pytest.fixture
def db(engine):
    with engine.begin() as c:
        c.execute(text(f"truncate {DATA_TABLES} cascade"))
    yield engine
    with engine.begin() as c:
        c.execute(text(f"truncate {DATA_TABLES} cascade"))


class World:
    def __init__(self, engine):
        self.engine = engine
        self.n = 0

    def account(self, mode="AUTO", state="CONNECTED", test_mode=False, short="acc"):
        with self.engine.begin() as c:
            return c.execute(
                text(
                    """insert into platform_accounts (platform_key, short_name, display_name, mode,
                           state, test_mode) values ('facebook_page', :s, :s, :m, :st, :tm)
                       returning id"""
                ),
                {"s": short, "m": mode, "st": state, "tm": test_mode},
            ).scalar_one()

    def queue(self, account, status="ACTIVE", name="Main"):
        with self.engine.begin() as c:
            return c.execute(
                text(
                    """insert into queues (account_id, name, start_at, recurrence, status)
                       values (:a, :n, :t, cast(:r as jsonb), :s) returning id"""
                ),
                {
                    "a": account,
                    "n": name,
                    "t": T0,
                    "r": '{"type": "interval_days", "every": 7}',
                    "s": status,
                },
            ).scalar_one()

    def post(
        self,
        account,
        queue,
        slot_at=T0,
        status="SCHEDULED",
        caption="hello",
        approved=True,
        post_type="text",
        hash_ok=True,
        media=False,
    ):
        self.n += 1
        cid = f"C{self.n:03d}"
        pid = f"{cid}-FB"
        with self.engine.begin() as c:
            c.execute(
                text("insert into content_items (content_id, title) values (:c, 't')"), {"c": cid}
            )
            h = approval_hash(
                {
                    "post_type": post_type,
                    "language": "en",
                    "title": None,
                    "caption": caption,
                    "link_url": None,
                    "hashtags": [],
                    "media_sha256": [],
                    "account_id": str(account),
                }
            )
            c.execute(
                text(
                    """insert into posts (post_id, content_id, account_id, queue_id, post_type, caption,
                           status, approved_at, approved_hash)
                       values (:p, :c, :a, :q, :t, :cap, :st, :ap, :h)"""
                ),
                {
                    "p": pid,
                    "c": cid,
                    "a": account,
                    "q": queue,
                    "t": post_type,
                    "cap": caption,
                    "st": status,
                    "ap": T0 - timedelta(days=1) if approved else None,
                    "h": h if hash_ok else "stale-hash",
                },
            )
            c.execute(
                text(
                    """insert into queue_slots (queue_id, slot_at, post_id, state, filled_by)
                       values (:q, :t, :p, 'FILLED', 'FIFO')"""
                ),
                {"q": queue, "t": slot_at, "p": pid},
            )
        return pid

    def status(self, pid):
        with self.engine.connect() as c:
            return c.execute(
                text("select status from posts where post_id=:p"), {"p": pid}
            ).scalar_one()

    def row(self, pid):
        with self.engine.connect() as c:
            return dict(
                c.execute(text("select * from posts where post_id=:p"), {"p": pid}).mappings().one()
            )

    def attempts(self, pid):
        with self.engine.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    text(
                        "select * from publish_attempts where post_id=:p order by started_at, attempt_no"
                    ),
                    {"p": pid},
                ).mappings()
            ]

    def scalar(self, sql, **p):
        with self.engine.connect() as c:
            return c.execute(text(sql), p).scalar_one()


@pytest.fixture
def w(db):
    return World(db)


def run(engine, adapter, now, **kw):
    return run_once(engine, lambda settings: adapter, now, **kw)


def test_due_approved_post_is_published(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter()
    s = run(w.engine, a, T0 + timedelta(minutes=5))
    assert s.published == [pid] and w.status(pid) == "PUBLISHED"
    r = w.row(pid)
    assert (
        r["platform_post_id"].startswith("mock-") and r["published_url"] and r["locked_by"] is None
    )
    assert [x["result"] for x in w.attempts(pid)] == ["SUCCESS"]
    assert w.scalar("select state from queue_slots where post_id=:p", p=pid) == "DONE"
    assert w.scalar("select count(*) from job_runs where job='publisher' and ok") == 1


def test_not_due_post_untouched(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter()
    s = run(w.engine, a, T0 - timedelta(minutes=1))
    assert s.published == [] and a.calls == 0 and w.status(pid) == "SCHEDULED"


def test_unapproved_post_never_published_even_in_a_slot(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc), status="DRAFT", approved=False)
    a = MockAdapter()
    run(w.engine, a, T0 + timedelta(minutes=5))
    assert a.calls == 0 and w.status(pid) == "DRAFT"


def test_kill_switch_stops_everything(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    with w.engine.begin() as c:
        c.execute(text("insert into settings (key, value) values ('kill_switch', 'true'::jsonb)"))
    a = MockAdapter()
    s = run(w.engine, a, T0 + timedelta(minutes=5))
    assert s.skipped_kill_switch and a.calls == 0 and w.status(pid) == "SCHEDULED"


def test_paused_queue_and_disabled_account_are_skipped(w):
    acc = w.account(short="a1")
    p1 = w.post(acc, w.queue(acc, status="PAUSED"))
    acc2 = w.account(state="DISABLED", short="a2")
    p2 = w.post(acc2, w.queue(acc2))
    a = MockAdapter()
    run(w.engine, a, T0 + timedelta(minutes=5))
    assert a.calls == 0 and w.status(p1) == w.status(p2) == "SCHEDULED"


def test_concurrent_runs_publish_each_post_once(w):
    acc = w.account()
    q = w.queue(acc)
    pids = [w.post(acc, q, slot_at=T0 + timedelta(minutes=i)) for i in range(5)]
    a = MockAdapter()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        run(w.engine, a, T0 + timedelta(hours=1))

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert a.calls == 5, "each post must be sent to the platform exactly once"
    assert all(w.status(p) == "PUBLISHED" for p in pids)
    assert all(len(w.attempts(p)) == 1 for p in pids)


def test_temporary_failure_retries_on_policy_then_final(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter({"outcome": "temporary"})
    t = T0 + timedelta(minutes=1)
    s = run(w.engine, a, t)
    assert s.retry_scheduled == [pid] and w.status(pid) == "RETRYING"
    assert w.row(pid)["next_retry_at"] == t + timedelta(minutes=15)

    # too early: nothing happens
    assert run(w.engine, a, t + timedelta(minutes=10)).retry_scheduled == []
    calls = a.calls

    t2 = t + timedelta(minutes=16)
    run(w.engine, a, t2)
    assert w.row(pid)["next_retry_at"] == t2 + timedelta(hours=1)
    t3 = t2 + timedelta(hours=1, minutes=1)
    run(w.engine, a, t3)
    assert w.row(pid)["next_retry_at"] == t3 + timedelta(hours=6)
    t4 = t3 + timedelta(hours=6, minutes=1)
    s = run(w.engine, a, t4, grace_window=timedelta(days=2))
    assert s.failed == [pid] and w.status(pid) == "FAILED_FINAL"
    assert ("FINAL_FAILURE", pid) in s.notifications
    assert a.calls == calls + 3
    assert [x["result"] for x in w.attempts(pid)] == ["FAILED"] * 4


def test_recovers_after_transient_failures(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter({"outcome": "temporary", "fail_n_times": 1})
    t = T0 + timedelta(minutes=1)
    run(w.engine, a, t)
    run(w.engine, a, t + timedelta(minutes=16))
    assert w.status(pid) == "PUBLISHED"
    assert [x["result"] for x in w.attempts(pid)] == ["FAILED", "SUCCESS"]


def test_auth_failure_pauses_account_and_stops_retrying(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter({"outcome": "auth"})
    s = run(w.engine, a, T0 + timedelta(minutes=1))
    assert ("TOKEN_EXPIRED", pid) in s.notifications
    assert w.scalar("select state from platform_accounts") == "TOKEN_EXPIRED"
    assert w.status(pid) == "SCHEDULED" and w.row(pid)["next_retry_at"] is None
    calls = a.calls
    s2 = run(w.engine, a, T0 + timedelta(minutes=30))
    assert a.calls == calls and s2.blocked and s2.published == []  # no retries until reconnect


@pytest.mark.parametrize("outcome", ["validation", "permanent"])
def test_non_retryable_failures_go_final_immediately(w, outcome):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter({"outcome": outcome})
    s = run(w.engine, a, T0 + timedelta(minutes=1))
    assert s.failed == [pid] and w.status(pid) == "FAILED_FINAL" and a.calls == 1
    run(w.engine, a, T0 + timedelta(hours=2))
    assert a.calls == 1


def test_rate_limit_waits_until_reset(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter({"outcome": "rate_limit", "retry_after_s": 7200, "fail_n_times": 1})
    t = T0 + timedelta(minutes=1)
    run(w.engine, a, t)
    assert w.row(pid)["next_retry_at"] == t + timedelta(hours=2)


def test_caption_over_limit_is_never_sent(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc), caption="x" * 50)
    a = MockAdapter({"caption_limit": 10})
    s = run(w.engine, a, T0 + timedelta(minutes=1))
    assert s.failed == [pid] and a.calls == 0
    assert w.attempts(pid)[0]["error_code"] == "E032"


def test_stale_slot_beyond_grace_window_becomes_overdue_not_published(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter()
    s = run(w.engine, a, T0 + timedelta(hours=7))
    assert s.overdue == [pid] and a.calls == 0 and w.status(pid) == "OVERDUE"


def test_hash_mismatch_blocks_publishing(w):
    acc = w.account()
    pid = w.post(acc, w.queue(acc), hash_ok=False)
    a = MockAdapter()
    s = run(w.engine, a, T0 + timedelta(minutes=1))
    assert s.failed == [pid] and a.calls == 0
    assert w.attempts(pid)[0]["error_code"] == "HASH_MISMATCH"


def test_assisted_account_delivers_package_and_awaits_confirmation(w):
    acc = w.account(mode="ASSISTED", state="NOT_CONFIGURED")
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter()
    s = run(w.engine, a, T0 + timedelta(minutes=1))
    assert s.delivered == [pid] and a.calls == 0  # never calls the platform API
    assert w.status(pid) == "AWAITING_CONFIRMATION"
    assert w.scalar("select state from assisted_tasks") == "DELIVERED"
    assert [x["attempt_type"] for x in w.attempts(pid)] == ["ASSISTED_DELIVERY"]
    # not delivered again on the next cycle
    assert run(w.engine, a, T0 + timedelta(minutes=20)).delivered == []


def test_test_mode_account_never_hits_real_platform(w):
    acc = w.account(test_mode=True)
    pid = w.post(acc, w.queue(acc))
    a = MockAdapter()
    run(w.engine, a, T0 + timedelta(minutes=1))
    assert a.published == {} and w.status(pid) == "PUBLISHED"


def test_one_failing_post_does_not_block_others(w):
    acc = w.account()
    q = w.queue(acc)
    bad = w.post(acc, q, slot_at=T0, caption="x" * 50)
    good = w.post(acc, q, slot_at=T0 + timedelta(minutes=1))
    a = MockAdapter({"caption_limit": 10})
    # shorten the good caption by re-creating it within the limit
    with w.engine.begin() as c:
        c.execute(text("update posts set caption='ok' where post_id=:p"), {"p": good})
    # keep approval hash valid for the edited caption
    h = approval_hash(
        {
            "post_type": "text",
            "language": "en",
            "title": None,
            "caption": "ok",
            "link_url": None,
            "hashtags": [],
            "media_sha256": [],
            "account_id": str(w.scalar("select account_id from posts where post_id=:p", p=good)),
        }
    )
    with w.engine.begin() as c:
        c.execute(text("update posts set approved_hash=:h where post_id=:p"), {"h": h, "p": good})
    s = run(w.engine, a, T0 + timedelta(minutes=5))
    assert s.failed == [bad] and s.published == [good]


def test_batch_limit_bounds_a_run(w):
    acc = w.account()
    q = w.queue(acc)
    pids = [w.post(acc, q, slot_at=T0 + timedelta(minutes=i)) for i in range(5)]
    a = MockAdapter()
    s = run(w.engine, a, T0 + timedelta(hours=1), batch_limit=2)
    assert len(s.published) == 2
    s = run(w.engine, a, T0 + timedelta(hours=1, minutes=15), batch_limit=10)
    assert len(s.published) == 3 and all(w.status(p) == "PUBLISHED" for p in pids)


# ----------------------------------------------------------- crash recovery (PUB-09)
def _make_stuck(w, locked_ago):
    acc = w.account()
    pid = w.post(acc, w.queue(acc))
    now = T0 + timedelta(hours=1)
    with w.engine.begin() as c:
        c.execute(
            text(
                "update posts set status='PUBLISHING', locked_by='dead-run', locked_at=:t where post_id=:p"
            ),
            {"t": now - locked_ago, "p": pid},
        )
    return pid, now


def test_stuck_publishing_post_found_on_platform_is_marked_published(w):
    pid, now = _make_stuck(w, timedelta(minutes=30))
    a = MockAdapter()
    slot_id = w.scalar("select id from queue_slots")
    a.publish(_view(pid), _ctx(f"{pid}:{slot_id}"))  # the crashed run's post exists remotely
    before = a.calls
    s = run(w.engine, a, now)
    assert w.status(pid) == "PUBLISHED" and s.published == [pid]
    assert a.calls == before, "must not publish a second time"


def test_stuck_publishing_post_not_found_needs_attention_and_is_not_republished(w):
    pid, now = _make_stuck(w, timedelta(minutes=30))
    a = MockAdapter()
    s = run(w.engine, a, now)
    assert w.status(pid) == "NEEDS_ATTENTION" and a.calls == 0
    assert ("NEEDS_ATTENTION", pid) in s.notifications


def test_recent_lock_is_left_alone(w):
    pid, now = _make_stuck(w, timedelta(minutes=2))
    a = MockAdapter()
    run(w.engine, a, now)
    assert w.status(pid) == "PUBLISHING" and a.calls == 0


def _view(pid):
    from socialcontrol.platforms.base import PostView

    return PostView(pid, "text", "en", "hello")


def _ctx(key):
    from socialcontrol.platforms.base import PublishContext

    return PublishContext(idempotency_key=key)
