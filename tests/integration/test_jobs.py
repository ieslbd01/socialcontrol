"""Jobs layer: publisher cycle with notifications, reminders, reports, watchdog, backup."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from socialcontrol import jobs
from socialcontrol.backup import BackupError, decrypt_bytes, dump_command, encrypt_bytes, run_backup
from socialcontrol.notifications.router import Router
from socialcontrol.platforms.adapters.assisted import AssistedAdapter
from socialcontrol.platforms.adapters.mock import MockAdapter
from socialcontrol.platforms.registry import adapter_for
from socialcontrol.review import service as review

pytestmark = pytest.mark.integration

START = datetime(2026, 10, 1, 4, tzinfo=UTC)
BEFORE = datetime(2026, 9, 30, 12, tzinfo=UTC)
KEY = "k" * 40
BASE = "http://dash.test"


class Fake:
    def __init__(self, name, ok=True):
        self.name, self.ok, self.sent = name, ok, []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return self.ok


def router(**kw):
    return Router({"telegram": Fake("telegram"), "email": Fake("email")}, **kw), kw


@pytest.fixture
def env(clean_db):
    with clean_db.begin() as c:
        acc = c.execute(
            text(
                """insert into platform_accounts (platform_key, short_name, display_name, mode, state, destination_url)
               values ('whatsapp_channel','wa','IESL WhatsApp','ASSISTED','CONNECTED','https://wa.me/channel/x') returning id"""
            )
        ).scalar_one()
        q = c.execute(
            text(
                """insert into queues (account_id, name, start_at, recurrence, is_default)
               values (:a,'Main',:s,cast(:r as jsonb),true) returning id"""
            ),
            {
                "a": acc,
                "s": START,
                "r": json.dumps({"type": "interval_days", "every": 7, "time_local": "10:00"}),
            },
        ).scalar_one()
    return clean_db, acc, q


def add_post(engine, acc, q, n, caption="Hello team", ptype="text", lang="en"):
    pid = f"C{n:03d}-WA"
    with engine.begin() as c:
        c.execute(
            text("insert into content_items (content_id,title) values (:c,'t')"), {"c": f"C{n:03d}"}
        )
        c.execute(
            text(
                """insert into posts (post_id,content_id,account_id,queue_id,post_type,caption,language,status,queue_position,hashtags)
               values (:p,:c,:a,:q,:t,:cap,:l,'DRAFT',:n,:h)"""
            ),
            {
                "p": pid,
                "c": f"C{n:03d}",
                "a": acc,
                "q": q,
                "t": ptype,
                "cap": caption,
                "l": lang,
                "n": n,
                "h": ["#iesl"],
            },
        )
    with engine.begin() as c:
        review.approve(c, pid, BEFORE)
    return pid


def test_assisted_cycle_sends_package_with_confirm_link_then_reminders(env):
    engine, acc, q = env
    pid = add_post(engine, acc, q, 1, caption="তাপমাত্রা ক্যালিব্রেশন 🌡️", lang="bn")
    r, _ = router()
    now = START + timedelta(minutes=3)
    s = jobs.run_publisher_cycle(engine, adapter_for, r, now, KEY, BASE)
    assert s.delivered == [pid]
    tg = r.notifiers["telegram"]
    subject, body = tg.sent[0]
    assert pid in subject and "তাপমাত্রা ক্যালিব্রেশন" in body and "#iesl" in body
    assert "https://wa.me/channel/x" in body
    link = re.search(rf"{BASE}/a/(\S+)", body).group(1)
    assert link

    # reminder at +2h reuses the SAME link, so the first message stays valid
    jobs.run_publisher_cycle(engine, adapter_for, r, now + timedelta(hours=2, minutes=5), KEY, BASE)
    reminder_body = tg.sent[-1][1]
    assert f"{BASE}/a/{link}" in reminder_body and "Reminder 1" in tg.sent[-1][0]


def test_final_failure_alert_goes_to_both_channels_with_reason(env):
    engine, acc, q = env
    with engine.begin() as c:
        c.execute(text("update platform_accounts set mode='AUTO'"))
    pid = add_post(engine, acc, q, 1)
    with engine.begin() as c:
        c.execute(text("update queues set runway_threshold_days=0"))
    r, _ = router()
    a = MockAdapter({"outcome": "validation"})
    jobs.run_publisher_cycle(engine, lambda s: a, r, START + timedelta(minutes=2), KEY, BASE)
    for name in ("telegram", "email"):
        sent = r.notifiers[name].sent
        assert (
            sent
            and pid in sent[0][0]
            and "VALIDATION" in sent[0][1]
            and f"{BASE}/posts/{pid}" in sent[0][1]
        )


def test_successful_publish_is_digested_not_sent_immediately(env):
    engine, acc, q = env
    with engine.begin() as c:
        c.execute(text("update platform_accounts set mode='AUTO'"))
    add_post(engine, acc, q, 1)
    with engine.begin() as c:
        c.execute(text("update queues set runway_threshold_days=0"))
    r, _ = router()
    jobs.run_publisher_cycle(
        engine, lambda s: MockAdapter(), r, START + timedelta(minutes=2), KEY, BASE
    )
    assert r.notifiers["telegram"].sent == []


def test_auto_account_without_real_adapter_falls_back_to_assisted_delivery(env):
    """AUTO set before any API adapter exists must never fail or publish: it is delivered by hand."""
    engine, acc, q = env
    with engine.begin() as c:
        c.execute(text("update platform_accounts set mode='AUTO'"))
    pid = add_post(engine, acc, q, 1)
    r, _ = router()
    s = jobs.run_publisher_cycle(engine, adapter_for, r, START + timedelta(minutes=2), KEY, BASE)
    assert s.delivered == [pid] and s.published == []


def test_registry_returns_assisted_adapter_by_default():
    a = adapter_for({"_platform_key": "linkedin_company", "_mode": "AUTO"})
    assert isinstance(a, AssistedAdapter) and a.supports_auto is False
    pkg = a.build_assisted_package(
        __import__("socialcontrol.platforms.base", fromlist=["PostView"]).PostView(
            "C1-LI", "text", "en", "Hi", hashtags=("#a",)
        )
    )
    assert pkg.char_limit == 3000 and pkg.hints and "#a" in pkg.text


def test_daily_report_job_stores_and_sends_summary(env):
    engine, acc, q = env
    add_post(engine, acc, q, 1)
    r, _ = router()
    jobs.run_publisher_cycle(engine, adapter_for, r, START + timedelta(minutes=2), KEY, BASE)
    r2, _ = router()
    after = START + timedelta(days=1, hours=3)
    rep = jobs.run_report(engine, r2, "daily", after)
    assert rep.summary["assisted_delivered"] == 1
    assert (
        r2.notifiers["telegram"].sent
        and "daily report" in r2.notifiers["telegram"].sent[0][0].lower()
    )
    with engine.connect() as c:
        assert c.execute(text("select count(*) from reports where kind='daily'")).scalar_one() == 1


def test_watchdog_alerts_when_publisher_is_silent_and_is_quiet_when_healthy(env):
    engine, acc, q = env
    r, _ = router()
    now = START + timedelta(hours=3)
    problems = jobs.watchdog(engine, r, now)  # never ran
    assert "HEARTBEAT_MISSED" in problems and r.notifiers["telegram"].sent
    jobs.run_publisher_cycle(engine, adapter_for, r, now, KEY, BASE)
    healthy = Router({"telegram": Fake("telegram")})
    add_post(engine, acc, q, 1)
    with engine.begin() as c:  # plenty of runway
        c.execute(text("update queues set runway_threshold_days=0"))
    assert jobs.watchdog(engine, healthy, now + timedelta(minutes=10)) == []
    assert healthy.notifiers["telegram"].sent == []


def test_watchdog_reports_expired_tokens_and_low_runway(env):
    engine, acc, q = env
    with engine.begin() as c:
        c.execute(text("update platform_accounts set state='TOKEN_EXPIRED'"))
    r, _ = router()
    now = START + timedelta(minutes=1)
    jobs.run_publisher_cycle(engine, adapter_for, r, now, KEY, BASE)
    problems = jobs.watchdog(
        engine, Router({"telegram": Fake("telegram")}), now + timedelta(minutes=5)
    )
    assert "TOKEN_EXPIRED" in problems and any(p.startswith("RUNWAY_LOW") for p in problems)


# ---------------------------------------------------------------- backup
def test_backup_encrypt_round_trip_and_wrong_key():
    key = Fernet.generate_key().decode()
    blob = encrypt_bytes(b"secret dump bytes" * 20, key)
    assert b"secret" not in blob and decrypt_bytes(blob, key) == b"secret dump bytes" * 20
    with pytest.raises(BackupError):
        decrypt_bytes(blob, Fernet.generate_key().decode())
    with pytest.raises(BackupError, match="unencrypted"):
        encrypt_bytes(b"x", "")


def test_dump_command_never_contains_the_password():
    cmd = dump_command("postgresql+psycopg://user:s3cret@db.example.com:6543/socialcontrol")
    assert "s3cret" not in " ".join(cmd) and "--host" in cmd and "db.example.com" in cmd
    assert cmd[cmd.index("--dbname") + 1] == "socialcontrol"


def test_run_backup_writes_encrypted_file_prunes_and_passes_password_via_env(tmp_path):
    key = Fernet.generate_key().decode()
    seen = {}

    def fake_runner(cmd, env):
        seen["env"] = env
        return b"PGDMP" + b"x" * 500

    for day in range(1, 12):
        run_backup(
            "postgresql+psycopg://u:pw@h:5432/d",
            key,
            tmp_path,
            datetime(2026, 10, day, tzinfo=UTC),
            keep=8,
            runner=fake_runner,
        )
    files = sorted(p.name for p in tmp_path.glob("*.dump.enc"))
    assert (
        len(files) == 8 and files[0] == "2026-10-04.dump.enc" and files[-1] == "2026-10-11.dump.enc"
    )
    assert seen["env"]["PGPASSWORD"] == "pw"
    assert decrypt_bytes((tmp_path / files[-1]).read_bytes(), key).startswith(b"PGDMP")


def test_tiny_dump_is_refused(tmp_path):
    with pytest.raises(BackupError, match="small"):
        run_backup(
            "postgresql+psycopg://u:p@h/d",
            Fernet.generate_key().decode(),
            tmp_path,
            datetime(2026, 10, 1, tzinfo=UTC),
            runner=lambda c, e: b"x",
        )


def test_runway_low_message_names_the_queue_and_links_to_queues_page(env):
    engine, acc, q = env
    r, _ = router()
    jobs.run_publisher_cycle(engine, adapter_for, r, BEFORE + timedelta(hours=1), KEY, BASE)
    subject, body = r.notifiers["telegram"].sent[0]
    assert "running low" in subject and "'Main'" in body and f"{BASE}/queues" in body
    assert str(q) not in body
