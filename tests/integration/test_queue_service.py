"""Queue service tests against Postgres: PRD-05 / TST-01 T-QUE-*."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from socialcontrol.scheduler import queue_service as qs

pytestmark = pytest.mark.integration

START = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)  # 10:00 Dhaka
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class Env:
    def __init__(self, engine):
        self.e = engine
        self.n = 0
        with engine.begin() as c:
            self.acc = c.execute(
                text(
                    """insert into platform_accounts (platform_key, short_name, display_name, mode, state)
                       values ('facebook_page', 'p', 'p', 'ASSISTED', 'CONNECTED') returning id"""
                )
            ).scalar_one()

    def queue(self, name="Main", every=7, pattern=(), mode="RELAXED", **kw):
        rec = json.dumps({"type": "interval_days", "every": every, "time_local": "10:00"})
        cols = {
            "account_id": self.acc,
            "name": name,
            "start_at": START,
            "recurrence": rec,
            "pattern": json.dumps(list(pattern)),
            "pattern_mode": mode,
        }
        cols.update(kw)
        keys = ", ".join(cols)
        vals = ", ".join(
            f"cast(:{k} as jsonb)" if k in ("recurrence", "pattern") else f":{k}" for k in cols
        )
        with self.e.begin() as c:
            return c.execute(
                text(f"insert into queues ({keys}) values ({vals}) returning id"), cols
            ).scalar_one()

    def post(
        self,
        queue,
        status="APPROVED",
        ptype="text",
        evergreen=False,
        pos=None,
        pid=None,
        published_at=None,
    ):
        self.n += 1
        cid = f"C{self.n:03d}"
        pid = pid or f"{cid}-FB"
        with self.e.begin() as c:
            c.execute(
                text("insert into content_items (content_id, title) values (:c, 't')"), {"c": cid}
            )
            c.execute(
                text(
                    """insert into posts (post_id, content_id, account_id, queue_id, post_type, caption,
                           status, approved_at, evergreen, queue_position, published_at, approved_hash)
                       values (:p, :c, :a, :q, :t, 'x', :s, :ap, :ev, :pos, :pub, 'h')"""
                ),
                {
                    "p": pid,
                    "c": cid,
                    "a": self.acc,
                    "q": queue,
                    "t": ptype,
                    "s": status,
                    "ap": NOW if status != "DRAFT" else None,
                    "ev": evergreen,
                    "pos": pos if pos is not None else self.n,
                    "pub": published_at,
                },
            )
        return pid

    def run(self, fn, *a, **k):
        with self.e.begin() as c:
            return fn(c, *a, **k)

    def slots(self, queue):
        with self.e.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    text(
                        "select slot_at, state, post_id, filled_by from queue_slots where queue_id=:q order by slot_at"
                    ),
                    {"q": queue},
                ).mappings()
            ]

    def sched(self, queue):
        return [s["post_id"] for s in self.slots(queue) if s["state"] == "FILLED"]

    def status(self, pid):
        with self.e.connect() as c:
            return c.execute(
                text("select status from posts where post_id=:p"), {"p": pid}
            ).scalar_one()

    def sched_at(self, pid):
        with self.e.connect() as c:
            return c.execute(
                text("select scheduled_at from posts where post_id=:p"), {"p": pid}
            ).scalar_one()


@pytest.fixture
def env(clean_db):
    return Env(clean_db)


def test_ensure_horizon_creates_weekly_slots_and_is_idempotent(env):
    q = env.queue()
    created = env.run(qs.ensure_horizon, q, NOW)
    assert 12 <= created <= 14  # ~90 days / 7
    assert env.run(qs.ensure_horizon, q, NOW) == 0
    first = env.slots(q)[0]["slot_at"]
    assert first == START
    gaps = {
        (b["slot_at"] - a["slot_at"]).days
        for a, b in zip(env.slots(q), env.slots(q)[1:], strict=False)
    }
    assert gaps == {7}


def test_max_posts_and_end_at_limit_slots(env):
    q1 = env.queue(name="A", max_posts=3)
    assert env.run(qs.ensure_horizon, q1, NOW) == 3
    q2 = env.queue(name="B", end_at=START + timedelta(days=15))
    assert env.run(qs.ensure_horizon, q2, NOW) == 3  # day 0, 7, 14


def test_allocation_and_continuation(env):
    q = env.queue()
    first = [env.post(q) for _ in range(5)]
    s = env.run(qs.allocate_queue, q, NOW)
    assert s.scheduled == sorted(first) and s.unassigned == []
    assert env.sched(q) == first
    assert all(env.status(p) == "SCHEDULED" for p in first)
    times = [env.sched_at(p) for p in first]
    assert [(b - a).days for a, b in zip(times, times[1:], strict=False)] == [7, 7, 7, 7]

    second = [env.post(q) for _ in range(3)]  # later batch: no reconfiguration
    env.run(qs.allocate_queue, q, NOW)
    assert env.sched(q) == first + second
    assert env.sched_at(second[0]) - env.sched_at(first[-1]) == timedelta(days=7)
    # nothing already scheduled moved
    assert [env.sched_at(p) for p in first] == times


def test_unapproved_posts_are_never_slotted(env):
    q = env.queue()
    draft = env.post(q, status="DRAFT")
    review = env.post(q, status="IN_REVIEW")
    ok = env.post(q)
    env.run(qs.allocate_queue, q, NOW)
    assert env.sched(q) == [ok]
    assert env.status(draft) == "DRAFT" and env.status(review) == "IN_REVIEW"


def test_paused_queue_does_not_allocate(env):
    q = env.queue(status="PAUSED")
    p = env.post(q)
    env.run(qs.allocate_queue, q, NOW)
    assert env.status(p) == "APPROVED" and env.sched(q) == []


def test_independent_schedules_per_queue(env):
    weekly = env.queue(name="Weekly", every=7)
    threeday = env.queue(name="Three", every=3)
    a = env.post(weekly)
    b = env.post(weekly)
    c = env.post(threeday)
    d = env.post(threeday)
    env.run(qs.allocate_queue, weekly, NOW)
    env.run(qs.allocate_queue, threeday, NOW)
    assert env.sched_at(b) - env.sched_at(a) == timedelta(days=7)
    assert env.sched_at(d) - env.sched_at(c) == timedelta(days=3)


def test_pattern_relaxed_places_types_in_rhythm(env):
    q = env.queue(pattern=["video", "image"])
    posts = {
        "i1": env.post(q, ptype="image"),
        "v1": env.post(q, ptype="video"),
        "i2": env.post(q, ptype="image"),
        "v2": env.post(q, ptype="video"),
    }
    env.run(qs.allocate_queue, q, NOW)
    assert env.sched(q) == [posts["v1"], posts["i1"], posts["v2"], posts["i2"]]


def test_cancel_shifts_later_posts_up(env):
    q = env.queue()
    ps = [env.post(q) for _ in range(5)]
    env.run(qs.allocate_queue, q, NOW)
    original = {p: env.sched_at(p) for p in ps}
    env.run(qs.release_post, q, ps[2], NOW, "CANCELLED")
    assert env.status(ps[2]) == "CANCELLED"
    assert env.sched(q) == [ps[0], ps[1], ps[3], ps[4]]
    assert env.sched_at(ps[3]) == original[ps[2]] and env.sched_at(ps[4]) == original[ps[3]]
    assert env.sched_at(ps[0]) == original[ps[0]]
    # the slot at the end is open again, so a new post continues from there
    new = env.post(q)
    env.run(qs.allocate_queue, q, NOW)
    assert env.sched_at(new) == original[ps[4]]


def test_cancel_with_keep_holes_leaves_gap_then_fifo_fills_it(env):
    q = env.queue(keep_holes=True)
    ps = [env.post(q) for _ in range(4)]
    env.run(qs.allocate_queue, q, NOW)
    original = {p: env.sched_at(p) for p in ps}
    env.run(qs.release_post, q, ps[1], NOW, "CANCELLED")
    assert env.sched(q) == [ps[0], ps[2], ps[3]] and env.sched_at(ps[2]) == original[ps[2]]
    new = env.post(q)
    env.run(qs.allocate_queue, q, NOW)
    assert env.sched_at(new) == original[ps[1]]  # next free slot is the hole


def test_editing_an_approved_post_frees_its_slot(env):
    q = env.queue()
    ps = [env.post(q) for _ in range(3)]
    env.run(qs.allocate_queue, q, NOW)
    env.run(qs.release_post, q, ps[0], NOW, "IN_REVIEW")
    assert env.status(ps[0]) == "IN_REVIEW" and env.sched(q) == [ps[1], ps[2]]
    assert env.sched_at(ps[1]) == START


def test_runway_and_low_runway_detection(env):
    q = env.queue(runway_threshold_days=14)
    assert env.run(qs.runway_days, q, NOW) == 0
    assert [x[0] for x in env.run(qs.queues_low_on_runway, NOW)] == [str(q)]
    for _ in range(4):
        env.post(q)
    env.run(qs.allocate_queue, q, NOW)
    days = env.run(qs.runway_days, q, NOW)
    assert 20 < days < 22  # 4 weekly slots starting tomorrow-ish
    assert env.run(qs.queues_low_on_runway, NOW) == []


# ---------------------------------------------------------------- empty slots / evergreen
def due_time():
    return START + timedelta(hours=1)


def test_empty_slot_without_evergreen_is_marked_empty(env):
    q = env.queue()
    env.run(qs.ensure_horizon, q, NOW)
    out = env.run(qs.fill_empty_slots, q, due_time())
    assert len(out["empty"]) == 1 and out["evergreen"] == []
    assert env.slots(q)[0]["state"] == "EMPTY"


def test_evergreen_fills_empty_slot_with_oldest_eligible_clone(env):
    q = env.queue(evergreen_enabled=True, evergreen_gap_days=90)
    now = due_time()
    old = env.post(q, status="PUBLISHED", evergreen=True, published_at=now - timedelta(days=200))
    newer = env.post(q, status="PUBLISHED", evergreen=True, published_at=now - timedelta(days=120))
    env.post(
        q, status="PUBLISHED", evergreen=True, published_at=now - timedelta(days=10)
    )  # too recent
    env.run(qs.ensure_horizon, q, NOW)
    out = env.run(qs.fill_empty_slots, q, now)
    assert out["evergreen"] == [f"{old}-R1"]
    slot = env.slots(q)[0]
    assert (
        slot["state"] == "FILLED"
        and slot["filled_by"] == "EVERGREEN"
        and slot["post_id"] == f"{old}-R1"
    )
    assert env.status(f"{old}-R1") == "SCHEDULED"
    assert newer not in out["evergreen"]


def test_evergreen_never_picks_the_same_post_twice_in_a_row(env):
    q = env.queue(evergreen_enabled=True, evergreen_gap_days=30)
    now = START + timedelta(days=7, hours=1)  # two slots are due: day 0 and day 7
    a = env.post(q, status="PUBLISHED", evergreen=True, published_at=now - timedelta(days=300))
    b = env.post(q, status="PUBLISHED", evergreen=True, published_at=now - timedelta(days=200))
    env.run(qs.ensure_horizon, q, NOW)
    out = env.run(qs.fill_empty_slots, q, now - timedelta(days=7))  # first due slot only
    assert out["evergreen"] == [f"{a}-R1"]
    # the clone of A is now "previous"; next empty slot must not be A again
    out2 = env.run(qs.fill_empty_slots, q, now)
    assert out2["evergreen"] == [f"{b}-R1"]


def test_evergreen_respects_gap(env):
    q = env.queue(evergreen_enabled=True, evergreen_gap_days=90)
    now = due_time()
    env.post(q, status="PUBLISHED", evergreen=True, published_at=now - timedelta(days=45))
    env.run(qs.ensure_horizon, q, NOW)
    out = env.run(qs.fill_empty_slots, q, now)
    assert out["evergreen"] == [] and len(out["empty"]) == 1


def test_evergreen_disabled_ignores_pool(env):
    q = env.queue(evergreen_enabled=False)
    env.post(q, status="PUBLISHED", evergreen=True, published_at=due_time() - timedelta(days=365))
    env.run(qs.ensure_horizon, q, NOW)
    out = env.run(qs.fill_empty_slots, q, due_time())
    assert out["evergreen"] == [] and len(out["empty"]) == 1
