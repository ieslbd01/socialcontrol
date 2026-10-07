"""Review workflow tests (PRD-04 / T-REV-*)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from socialcontrol.platforms.adapters.mock import MockAdapter
from socialcontrol.publisher.engine import run_once
from socialcontrol.review import service as rv

pytestmark = pytest.mark.integration

START = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


class Env:
    def __init__(self, engine):
        self.e = engine
        self.n = 0
        with engine.begin() as c:
            self.acc = c.execute(
                text(
                    """insert into platform_accounts (platform_key, short_name, display_name, mode, state)
                       values ('facebook_page', 'p', 'p', 'AUTO', 'CONNECTED') returning id"""
                )
            ).scalar_one()
            self.q = c.execute(
                text(
                    """insert into queues (account_id, name, start_at, recurrence, is_default)
                       values (:a, 'Main', :t, cast(:r as jsonb), true) returning id"""
                ),
                {
                    "a": self.acc,
                    "t": START,
                    "r": json.dumps({"type": "interval_days", "every": 7, "time_local": "10:00"}),
                },
            ).scalar_one()

    def post(self, status="DRAFT", ptype="text", caption="Hello", queue=True, media=False):
        self.n += 1
        cid, pid = f"C{self.n:03d}", f"C{self.n:03d}-FB"
        with self.e.begin() as c:
            c.execute(
                text("insert into content_items (content_id, title) values (:c,'t')"), {"c": cid}
            )
            c.execute(
                text(
                    """insert into posts (post_id, content_id, account_id, queue_id, post_type, caption,
                           status, approved_at, queue_position)
                       values (:p,:c,:a,:q,:t,:cap,:s,:ap,:n)"""
                ),
                {
                    "p": pid,
                    "c": cid,
                    "a": self.acc,
                    "q": self.q if queue else None,
                    "t": ptype,
                    "cap": caption,
                    "s": status,
                    "ap": NOW if status not in ("DRAFT", "IN_REVIEW") else None,
                    "n": self.n,
                },
            )
            if media:
                mid = c.execute(
                    text(
                        """insert into media (sha256, filename, mime, bytes, backend, storage_key, public_url)
                           values (:h,'a.jpg','image/jpeg',10,'supabase',:k,'https://x/a.jpg') returning id"""
                    ),
                    {"h": f"{self.n:064x}", "k": f"k{self.n}"},
                ).scalar_one()
                c.execute(
                    text("insert into post_media (post_id, media_id) values (:p,:m)"),
                    {"p": pid, "m": mid},
                )
        return pid

    def run(self, fn, *a, **k):
        with self.e.begin() as c:
            return fn(c, *a, **k)

    def row(self, pid):
        with self.e.connect() as c:
            return dict(
                c.execute(text("select * from posts where post_id=:p"), {"p": pid}).mappings().one()
            )

    def audit(self, pid):
        with self.e.connect() as c:
            return [
                r[0]
                for r in c.execute(
                    text("select action from post_audit where post_id=:p order by id"), {"p": pid}
                )
            ]

    def sched(self):
        with self.e.connect() as c:
            return [
                r[0]
                for r in c.execute(
                    text("select post_id from queue_slots where state='FILLED' order by slot_at")
                )
            ]


@pytest.fixture
def env(clean_db):
    return Env(clean_db)


def test_approve_sets_hash_audit_and_schedules(env):
    pid = env.post()
    env.run(rv.approve, pid, NOW)
    r = env.row(pid)
    assert r["status"] == "SCHEDULED" and r["approved_at"] and r["scheduled_at"] == START
    with env.e.connect() as c:
        assert r["approved_hash"] == rv.compute_hash(c, r)
    assert "APPROVED" in env.audit(pid)


def test_cannot_approve_when_media_missing_or_wrong_status(env):
    needs_media = env.post(ptype="image")
    with pytest.raises(rv.ReviewError, match="media is required"):
        env.run(rv.approve, needs_media, NOW)
    assert env.row(needs_media)["status"] == "DRAFT"
    done = env.post(status="PUBLISHED")
    with pytest.raises(rv.ReviewError, match="cannot approve"):
        env.run(rv.approve, done, NOW)
    empty = env.post(caption="  ")
    with pytest.raises(rv.ReviewError, match="caption is required"):
        env.run(rv.approve, empty, NOW)


def test_caption_limit_blocks_approval(env):
    with env.e.begin() as c:
        c.execute(
            text(
                "update platform_capabilities set max_caption_chars=10 where platform_key='facebook_page' and post_type='text'"
            )
        )
    try:
        pid = env.post(caption="x" * 50)
        with pytest.raises(rv.ReviewError, match="limit 10"):
            env.run(rv.approve, pid, NOW)
    finally:
        with env.e.begin() as c:
            c.execute(
                text(
                    "update platform_capabilities set max_caption_chars=63000 where platform_key='facebook_page' and post_type='text'"
                )
            )


def test_reject_needs_comment_and_returns_to_draft(env):
    pid = env.post(status="IN_REVIEW")
    with pytest.raises(rv.ReviewError, match="comment"):
        env.run(rv.reject, pid, "  ")
    env.run(rv.reject, pid, "wrong stat in line 2")
    r = env.row(pid)
    assert r["status"] == "DRAFT" and "wrong stat" in r["notes"]


def test_submit_for_review(env):
    pid = env.post()
    env.run(rv.submit_for_review, pid)
    assert env.row(pid)["status"] == "IN_REVIEW"
    with pytest.raises(rv.ReviewError):
        env.run(rv.submit_for_review, pid)


def test_editing_approved_post_returns_to_review_and_frees_slot(env):
    a, b, c3 = env.post(), env.post(), env.post()
    for p in (a, b, c3):
        env.run(rv.approve, p, NOW)
    assert env.sched() == [a, b, c3]
    env.run(rv.edit_post, a, {"caption": "changed"}, NOW)
    r = env.row(a)
    assert r["status"] == "IN_REVIEW" and r["approved_at"] is None and r["approved_hash"] is None
    assert r["caption"] == "changed" and r["scheduled_at"] is None
    assert env.sched() == [b, c3] and env.row(b)["scheduled_at"] == START  # later posts shifted up
    assert {"EDITED", "RETURNED_TO_REVIEW"} <= set(env.audit(a))
    # must be re-approved before it can go back in the queue
    env.run(rv.approve, a, NOW)
    assert env.sched() == [b, c3, a]


def test_noop_edit_changes_nothing(env):
    pid = env.post()
    env.run(rv.approve, pid, NOW)
    env.run(rv.edit_post, pid, {"caption": "Hello"}, NOW)
    assert env.row(pid)["status"] == "SCHEDULED"


def test_edit_hashtags_detected_as_change(env):
    pid = env.post()
    env.run(rv.approve, pid, NOW)
    env.run(rv.edit_post, pid, {"hashtags": ["#a"]}, NOW)
    assert env.row(pid)["status"] == "IN_REVIEW"


def test_published_post_is_read_only_and_unknown_fields_rejected(env):
    done = env.post(status="PUBLISHED")
    with pytest.raises(rv.ReviewError, match="read-only"):
        env.run(rv.edit_post, done, {"caption": "x"}, NOW)
    draft = env.post()
    with pytest.raises(rv.ReviewError, match="cannot edit"):
        env.run(rv.edit_post, draft, {"status": "PUBLISHED"}, NOW)
    with pytest.raises(rv.ReviewError, match="cannot edit"):
        env.run(rv.edit_post, draft, {"approved_at": "2026-01-01"}, NOW)


def test_cancel_and_skip_shift_up(env):
    a, b, c3, d = (env.post() for _ in range(4))
    for p in (a, b, c3, d):
        env.run(rv.approve, p, NOW)
    env.run(rv.cancel, b, NOW)
    assert env.row(b)["status"] == "CANCELLED" and env.sched() == [a, c3, d]
    env.run(rv.cancel, c3, NOW, skip=True)
    assert env.row(c3)["status"] == "SKIPPED" and env.sched() == [a, d]
    done = env.post(status="PUBLISHED")
    with pytest.raises(rv.ReviewError):
        env.run(rv.cancel, done, NOW)


def test_bulk_approve_excludes_problem_posts_and_allocates_rest(env):
    ok1, ok2 = env.post(), env.post()
    bad = env.post(ptype="image")  # needs media
    with_media = env.post(ptype="image", media=True)
    res = env.run(rv.bulk_approve, [ok1, bad, ok2, with_media], NOW)
    assert res.approved == [ok1, ok2, with_media]
    assert list(res.excluded) == [bad] and "media" in res.excluded[bad]
    assert env.sched() == [ok1, ok2, with_media] and env.row(bad)["status"] == "DRAFT"


def test_approved_post_publishes_with_matching_hash_end_to_end(env):
    pid = env.post(ptype="text_image", media=True, caption="Real flow")
    env.run(rv.approve, pid, NOW)
    s = run_once(env.e, lambda _s: MockAdapter(), START + timedelta(minutes=5))
    assert s.published == [pid] and env.row(pid)["status"] == "PUBLISHED"


def test_tampering_after_approval_is_caught_by_publisher(env):
    pid = env.post()
    env.run(rv.approve, pid, NOW)
    with env.e.begin() as c:  # out-of-band change, bypassing the service
        c.execute(text("update posts set caption='sneaky' where post_id=:p"), {"p": pid})
    a = MockAdapter()
    s = run_once(env.e, lambda _s: a, START + timedelta(minutes=5))
    assert s.failed == [pid] and a.calls == 0


def test_manual_retry_restarts_grace_and_publishes(env):
    pid = env.post()
    env.run(rv.approve, pid, NOW)
    a = MockAdapter({"outcome": "validation"})
    run_once(env.e, lambda _s: a, START + timedelta(minutes=5))
    assert env.row(pid)["status"] == "FAILED_FINAL"
    later = START + timedelta(days=2)  # far beyond the 6 h grace window
    env.run(rv.retry_now, pid, later)
    assert env.row(pid)["status"] == "RETRYING"
    good = MockAdapter()
    s = run_once(env.e, lambda _s: good, later + timedelta(minutes=1))
    assert s.published == [pid]
    with pytest.raises(rv.ReviewError, match="nothing to retry"):
        env.run(rv.retry_now, pid, later)


def test_kill_switch_toggle_blocks_and_restores_publishing(env):
    pid = env.post()
    env.run(rv.approve, pid, NOW)
    env.run(rv.set_kill_switch, True)
    a = MockAdapter()
    due = START + timedelta(minutes=5)
    assert run_once(env.e, lambda _s: a, due).skipped_kill_switch and a.calls == 0
    env.run(rv.set_kill_switch, False)
    assert run_once(env.e, lambda _s: a, due + timedelta(minutes=1)).published == [pid]
