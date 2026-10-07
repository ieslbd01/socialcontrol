"""Assisted flow (PRD-08) and notification router (PRD-09) tests."""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from socialcontrol.assisted import service as ast
from socialcontrol.notifications.router import (
    DEFAULT_RULES,
    EmailNotifier,
    Router,
    TelegramNotifier,
    format_event,
    in_quiet_hours,
)
from socialcontrol.platforms.adapters.mock import MockAdapter
from socialcontrol.publisher.engine import run_once
from tests.integration.test_publisher import T0, World  # reuse the publisher fixtures

pytestmark = pytest.mark.integration
KEY = "k" * 32


@pytest.fixture
def w(clean_db):
    return World(clean_db)


def deliver(w):
    acc = w.account(mode="ASSISTED", state="NOT_CONFIGURED")
    pid = w.post(acc, w.queue(acc))
    s = run_once(w.engine, lambda _s: MockAdapter(), T0 + timedelta(minutes=1), signing_key=KEY)
    return pid, s.assisted_tokens[pid], T0 + timedelta(minutes=1)


# ---------------------------------------------------------------- tokens
def test_token_round_trip_and_tamper_detection():
    exp = datetime(2026, 10, 10, tzinfo=UTC)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    tok = ast.make_token(KEY, "task-1", exp)
    assert ast.parse_token(KEY, tok, now) == "task-1"
    with pytest.raises(ast.TokenError, match="signature"):
        ast.parse_token("x" * 32, tok, now)
    with pytest.raises(ast.TokenError):
        ast.parse_token(KEY, tok[:-3] + "AAA", now)
    with pytest.raises(ast.TokenError, match="expired"):
        ast.parse_token(KEY, tok, exp + timedelta(seconds=1))
    with pytest.raises(ast.TokenError, match="malformed"):
        ast.parse_token(KEY, "garbage", now)
    with pytest.raises(ValueError):
        ast.make_token("short", "t", exp)


# ---------------------------------------------------------------- assisted flow
def test_delivery_issues_a_token_and_page_data(w):
    pid, tok, now = deliver(w)
    assert w.status(pid) == "AWAITING_CONFIRMATION"
    with w.engine.begin() as c:
        data = ast.load_package_data(c, tok, KEY, now)
    assert data["post_id"] == pid and data["caption"] == "hello"


def test_mark_done_publishes_logs_and_link_is_single_use(w):
    pid, tok, now = deliver(w)
    with w.engine.begin() as c:
        assert ast.confirm(c, tok, KEY, now + timedelta(hours=1), "https://li.test/p/1") == pid
    r = w.row(pid)
    assert (
        r["status"] == "PUBLISHED"
        and r["confirmed_by_owner"]
        and r["published_url"] == "https://li.test/p/1"
    )
    assert [a["attempt_type"] for a in w.attempts(pid)] == ["ASSISTED_DELIVERY", "ASSISTED_CONFIRM"]
    assert w.scalar("select state from assisted_tasks") == "CONFIRMED"
    assert w.scalar("select state from queue_slots where post_id=:p", p=pid) == "DONE"
    with w.engine.begin() as c, pytest.raises(ast.TokenError, match="already used"):
        ast.confirm(c, tok, KEY, now + timedelta(hours=2))


def test_skip_marks_post_skipped(w):
    pid, tok, now = deliver(w)
    with w.engine.begin() as c:
        ast.skip(c, tok, KEY, now)
    assert w.status(pid) == "SKIPPED" and w.scalar("select state from assisted_tasks") == "SKIPPED"


def test_expired_link_is_refused(w):
    pid, tok, now = deliver(w)
    with w.engine.begin() as c, pytest.raises(ast.TokenError, match="expired"):
        ast.confirm(c, tok, KEY, now + timedelta(days=8))
    assert w.status(pid) == "AWAITING_CONFIRMATION"


def test_reminders_then_overdue_then_still_confirmable(w):
    pid, tok, now = deliver(w)
    with w.engine.begin() as c:
        assert ast.process_reminders(c, now + timedelta(hours=1)) == []
        r1 = ast.process_reminders(c, now + timedelta(hours=2, minutes=1))
        assert [(r.number, r.overdue) for r in r1] == [(1, False)]
    with w.engine.begin() as c:
        r2 = ast.process_reminders(c, now + timedelta(hours=6, minutes=1))
        assert [(r.number, r.overdue) for r in r2] == [(2, False)]
    with w.engine.begin() as c:
        r3 = ast.process_reminders(c, now + timedelta(hours=24, minutes=1))
        assert [(r.number, r.overdue) for r in r3] == [(3, True)]
    assert w.status(pid) == "OVERDUE"
    with w.engine.begin() as c:
        assert ast.process_reminders(c, now + timedelta(days=3)) == []  # no more nagging
    with w.engine.begin() as c:
        ast.confirm(c, tok, KEY, now + timedelta(days=2), None)  # owner can still confirm
    assert w.status(pid) == "PUBLISHED"


# ---------------------------------------------------------------- notification router
class Fake:
    def __init__(self, name, ok=True):
        self.name, self.ok, self.sent = name, ok, []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return self.ok


NOON = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)  # 12:00 Dhaka
NIGHT = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)  # 00:00 Dhaka


def test_quiet_hours_window():
    assert in_quiet_hours(NIGHT) and not in_quiet_hours(NOON)
    assert in_quiet_hours(datetime(2026, 10, 1, 0, 59, tzinfo=UTC))  # 06:59 Dhaka
    assert not in_quiet_hours(datetime(2026, 10, 1, 1, 0, tzinfo=UTC))  # 07:00 Dhaka


def test_critical_goes_to_all_channels(clean_db):
    tg, em = Fake("telegram"), Fake("email")
    r = Router({"telegram": tg, "email": em})
    with clean_db.begin() as c:
        res = r.dispatch(
            c, "FINAL_FAILURE", "C001-FB", *format_event("FINAL_FAILURE", "C001-FB", "why"), NIGHT
        )
    assert res.sent == ["telegram", "email"] and len(tg.sent) == len(em.sent) == 1


def test_dedup_window(clean_db):
    tg = Fake("telegram")
    r = Router({"telegram": tg, "email": Fake("email")})
    with clean_db.begin() as c:
        a = r.dispatch(c, "RUNWAY_LOW", "q1", "s", "b", NOON)
        b = r.dispatch(c, "RUNWAY_LOW", "q1", "s", "b", NOON + timedelta(hours=1))
        other = r.dispatch(c, "RUNWAY_LOW", "q2", "s", "b", NOON + timedelta(hours=1))
        later = r.dispatch(c, "RUNWAY_LOW", "q1", "s", "b", NOON + timedelta(hours=25))
    assert a.sent and b.held == "deduplicated" and other.sent and later.sent
    assert len(tg.sent) == 3


def test_quiet_hours_hold_noncritical_but_not_critical_or_assisted(clean_db):
    tg = Fake("telegram")
    r = Router({"telegram": tg, "email": Fake("email")})
    with clean_db.begin() as c:
        held = r.dispatch(c, "EMPTY_SLOT", "s1", "s", "b", NIGHT)
        crit = r.dispatch(c, "NEEDS_ATTENTION", "p1", "s", "b", NIGHT)
        act = r.dispatch(c, "ASSISTED_DUE", "p2", "s", "b", NIGHT)
    assert held.held == "quiet_hours" and crit.sent and act.sent


def test_info_events_are_digested_not_sent(clean_db):
    tg = Fake("telegram")
    r = Router({"telegram": tg})
    with clean_db.begin() as c:
        res = r.dispatch(c, "PUBLISH_SUCCESS", "p1", "s", "b", NOON)
    assert res.held == "digest" and tg.sent == []
    assert DEFAULT_RULES["PUBLISH_SUCCESS"].digest


def test_fallback_to_other_channel_and_banner(clean_db):
    down, em = Fake("telegram", ok=False), Fake("email")
    with clean_db.begin() as c:
        res = Router({"telegram": down, "email": em}).dispatch(
            c, "EMPTY_SLOT", "s1", "s", "b", NOON
        )
    # EMPTY_SLOT routes to telegram only; fallback picks email
    assert res.sent == ["email"]
    with clean_db.begin() as c:
        none = Router({"telegram": Fake("telegram", ok=False)}).dispatch(
            c, "EMPTY_SLOT", "s2", "s", "b", NOON
        )
        banners = c.execute(
            text("select count(*) from notification_logs where channel='banner'")
        ).scalar_one()
    assert none.sent == [] and banners == 1


def test_unknown_event_is_ignored(clean_db):
    with clean_db.begin() as c:
        assert Router({}).dispatch(c, "NOPE", "x", "s", "b", NOON).held == "disabled"


def test_message_templates_contain_post_and_detail():
    subject, body = format_event("FINAL_FAILURE", "C001-IG", "caption too long", "https://x/fix")
    assert "C001-IG" in subject and "caption too long" in body and "https://x/fix" in body


# ---------------------------------------------------------------- real notifier classes (no network)
def test_telegram_notifier_posts_to_bot_api():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), request.read().decode()
        return httpx.Response(200, json={"ok": True})

    n = TelegramNotifier("TOKEN123", "42", httpx.Client(transport=httpx.MockTransport(handler)))
    assert n.send("Hi", "তাপমাত্রা") is True
    assert "botTOKEN123/sendMessage" in seen["url"] and "42" in seen["body"]
    bad = TelegramNotifier(
        "T", "1", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    assert bad.send("a", "b") is False
    assert TelegramNotifier("", "").send("a", "b") is False


def test_email_notifier_sends_and_survives_smtp_errors():
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            sent.append(msg)

    n = EmailNotifier("smtp.test", 587, "u@x", "pw", "owner@x", FakeSMTP)
    assert n.send("Subj", "Body") and sent[0]["To"] == "owner@x"

    class Boom(FakeSMTP):
        def send_message(self, msg):
            raise smtplib.SMTPException("down")

    assert EmailNotifier("smtp.test", 587, "u", "p", "o@x", Boom).send("a", "b") is False
