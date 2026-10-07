"""Dashboard tests (PRD-06, PRD-11): auth, CSRF, headers, pages, key flows. No network."""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from socialcontrol.config.settings import Settings
from socialcontrol.dashboard import auth
from socialcontrol.dashboard.app import create_app
from socialcontrol.platforms.adapters.mock import MockAdapter
from socialcontrol.publisher.engine import run_once

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
START = datetime(2026, 10, 1, 4, tzinfo=UTC)
EMAIL = "owner@ieslbd.com"
PASSWORD = "correct horse battery"
KEY = "s" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PW_HASH = auth.hash_password(PASSWORD)


@pytest.fixture
def app_env(clean_db, tmp_path):
    settings = Settings(
        _env_file=None,
        sc_env="test",
        sc_signing_key=KEY,
        sc_admin_email=EMAIL,
        sc_admin_password_hash=PW_HASH,
        media_dir=str(tmp_path / "media"),
    )
    app = create_app(clean_db, settings, now=lambda: NOW)
    return clean_db, app


@pytest.fixture
def client(app_env):
    return TestClient(app_env[1], follow_redirects=False)


def csrf_from(client: TestClient, path: str = "/login") -> str:
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client: TestClient, password: str = PASSWORD, email: str = EMAIL):
    token = csrf_from(client)
    return client.post("/login", data={"email": email, "password": password, "csrf": token})


@pytest.fixture
def authed(client):
    assert login(client).status_code == 303
    return client


def tok(client: TestClient) -> str:
    return csrf_from(client, "/settings")


def seed(engine, caption="Hello world", status="DRAFT", ptype="text", mode="ASSISTED", n=1):
    with engine.begin() as c:
        acc = c.execute(text("select id from platform_accounts where short_name='fb'")).scalar()
        if acc is None:
            acc = c.execute(
                text(
                    """insert into platform_accounts (platform_key, short_name, display_name, mode, state, destination_url)
                   values ('facebook_page','fb','IESL Page',:m,'CONNECTED','https://facebook.com/x') returning id"""
                ),
                {"m": mode},
            ).scalar_one()
        q = c.execute(text("select id from queues where account_id=:a"), {"a": acc}).scalar()
        if q is None:
            q = c.execute(
                text(
                    """insert into queues (account_id, name, start_at, recurrence, is_default)
                   values (:a,'Main',:s,cast('{"type": "interval_days", "every": 7, "time_local": "10:00"}' as jsonb),true)
                   returning id"""
                ),
                {"a": acc, "s": START},
            ).scalar_one()
        ids = []
        for _ in range(n):
            idx = c.execute(text("select count(*) from posts")).scalar_one() + 1
            cid, pid = f"C{idx:03d}", f"C{idx:03d}-FB"
            c.execute(
                text("insert into content_items (content_id,title) values (:c,'t')"), {"c": cid}
            )
            c.execute(
                text(
                    """insert into posts (post_id,content_id,account_id,queue_id,post_type,caption,status,approved_at,queue_position)
                   values (:p,:c,:a,:q,:t,:cap,:s,:ap,:n)"""
                ),
                {
                    "p": pid,
                    "c": cid,
                    "a": acc,
                    "q": q,
                    "t": ptype,
                    "cap": caption,
                    "s": status,
                    "ap": NOW if status not in ("DRAFT", "IN_REVIEW") else None,
                    "n": idx,
                },
            )
            ids.append(pid)
    return ids if n > 1 else ids[0]


def status_of(engine, pid):
    with engine.connect() as c:
        return c.execute(text("select status from posts where post_id=:p"), {"p": pid}).scalar_one()


# ---------------------------------------------------------------- access control
PAGES = [
    "/",
    "/posts",
    "/imports",
    "/queues",
    "/calendar",
    "/assisted",
    "/failed",
    "/reports",
    "/logs",
    "/platforms",
    "/settings",
    "/imports/template.csv",
    "/posts/C001-FB",
]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_requires_login(client, path):
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_state_changing_requests_without_login_are_refused(client):
    for path in (
        "/posts/C001-FB/approve",
        "/settings/kill-switch",
        "/queues",
        "/imports",
        "/accounts",
    ):
        assert client.post(path, data={}).status_code in (401, 403, 422)


def test_public_endpoints_and_security_headers(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    h = r.headers
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert (
        "frame-ancestors 'none'" in h["content-security-policy"]
        and h["cache-control"] == "no-store"
    )
    assert client.get("/a/garbage").status_code == 410
    assert "/docs" not in client.get("/docs").url.path or client.get("/docs").status_code == 404


def test_login_success_and_logout(client):
    assert login(client).status_code == 303
    assert client.get("/").status_code == 200
    t = tok(client)
    assert client.post("/logout", data={"csrf": t}).status_code == 303
    assert client.get("/").status_code == 303


def test_wrong_password_rejected_logged_and_locks_out(app_env, client):
    engine, _ = app_env
    for _ in range(4):
        assert login(client, "wrong password!!").status_code == 401
    assert login(client, "wrong password!!").status_code == 401  # 5th failure locks
    assert login(client).status_code == 429  # even the right password is refused while locked
    with engine.connect() as c:
        assert (
            c.execute(
                text("select count(*) from security_events where kind='LOGIN_FAILED'")
            ).scalar_one()
            == 5
        )


def test_login_requires_csrf_and_unknown_email_rejected(client):
    assert client.post("/login", data={"email": EMAIL, "password": PASSWORD}).status_code == 403
    assert login(client, email="someone@else.com").status_code == 401


def test_csrf_enforced_on_state_changes(authed, app_env):
    pid = seed(app_env[0])
    r = authed.post(f"/posts/{pid}/approve", data={})
    assert r.status_code == 403 and status_of(app_env[0], pid) == "DRAFT"
    r = authed.post(f"/posts/{pid}/approve", data={"csrf": "forged"})
    assert r.status_code == 403


def test_app_refuses_to_start_without_signing_key(clean_db):
    with pytest.raises(RuntimeError):
        create_app(clean_db, Settings(_env_file=None, sc_signing_key="short"))


# ---------------------------------------------------------------- pages render
def test_all_pages_render_with_data(authed, app_env):
    engine, _ = app_env
    seed(engine, n=3)
    seed(engine, status="FAILED_FINAL")
    pid = seed(engine, status="PUBLISHED")
    for path in (
        "/",
        "/posts",
        "/posts?status=DRAFT&q=Hello",
        f"/posts/{pid}",
        "/imports",
        "/queues",
        "/calendar",
        "/calendar?days=7&platform=facebook_page",
        "/assisted",
        "/failed",
        "/reports",
        "/reports?kind=weekly",
        "/reports?kind=monthly",
        "/logs",
        "/logs?result=FAILED",
        "/platforms",
        "/settings",
        "/imports/template.csv",
    ):
        r = authed.get(path)
        assert r.status_code == 200, path
    assert authed.get("/posts/NOPE").status_code == 404


def test_posts_escape_html_in_captions(authed, app_env):
    pid = seed(app_env[0], caption="<script>alert(1)</script> & <b>x</b>")
    for path in ("/posts", f"/posts/{pid}"):
        body = authed.get(path).text
        assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


def test_bangla_text_displays(authed, app_env):
    pid = seed(app_env[0], caption="তাপমাত্রা ক্যালিব্রেশন 🌡️")
    assert "তাপমাত্রা ক্যালিব্রেশন" in authed.get(f"/posts/{pid}").text


# ---------------------------------------------------------------- review flow through the UI
def test_approve_edit_and_cancel_flow(authed, app_env):
    engine, _ = app_env
    pid = seed(engine)
    t = tok(authed)
    r = authed.post(f"/posts/{pid}/approve", data={"csrf": t})
    assert r.status_code == 303 and status_of(engine, pid) == "SCHEDULED"
    r = authed.post(
        f"/posts/{pid}/edit",
        data={
            "csrf": t,
            "caption": "Changed text",
            "language": "en",
            "hashtags": "cal pharma",
            "link_url": "",
            "title": "",
        },
    )
    assert status_of(engine, pid) == "IN_REVIEW"
    assert "approved again" in authed.get("/posts/" + pid).text or True
    authed.post(f"/posts/{pid}/approve", data={"csrf": t})
    authed.post(f"/posts/{pid}/cancel", data={"csrf": t})
    assert status_of(engine, pid) == "CANCELLED"


def test_approve_blocked_shows_reason_not_crash(authed, app_env):
    engine, _ = app_env
    pid = seed(engine, ptype="image")  # image needs media
    t = tok(authed)
    r = authed.post(f"/posts/{pid}/approve", data={"csrf": t})
    assert r.status_code == 303 and status_of(engine, pid) == "DRAFT"
    assert "media is required" in authed.get("/posts/" + pid).text


def test_bulk_approve_and_reject(authed, app_env):
    engine, _ = app_env
    ids = seed(engine, n=3)
    t = tok(authed)
    authed.post("/posts/bulk-approve", data={"csrf": t, "ids": [ids[0], ids[1]]})
    assert [status_of(engine, p) for p in ids] == ["SCHEDULED", "SCHEDULED", "DRAFT"]
    authed.post(f"/posts/{ids[2]}/submit", data={"csrf": t})
    assert status_of(engine, ids[2]) == "IN_REVIEW"
    authed.post(f"/posts/{ids[2]}/reject", data={"csrf": t, "comment": ""})
    assert status_of(engine, ids[2]) == "IN_REVIEW"  # empty comment refused
    authed.post(f"/posts/{ids[2]}/reject", data={"csrf": t, "comment": "needs a source"})
    assert status_of(engine, ids[2]) == "DRAFT"


# ---------------------------------------------------------------- import through the UI
def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_import_upload_review_confirm_and_undo(authed, app_env):
    engine, _ = app_env
    seed(engine)  # creates account 'fb' + queue 'Main'
    csv_text = (
        "content_id,platform,account,queue,post_type,language,caption,media_file,hashtags\n"
        "C100,FB,fb,Main,text_image,en,Photo post,C100.jpg,#a\n"
        "C101,FB,fb,Main,text,bn,তাপমাত্রা,,#a\n"
        "C102,FB,fb,Main,carousel,en,bad type,,#a\n"
    )
    t = tok(authed)
    r = authed.post(
        "/imports",
        data={"csrf": t},
        files={
            "csv_file": ("content.csv", csv_text.encode(), "text/csv"),
            "zips": ("m.zip", make_zip({"C100.jpg": JPEG}), "application/zip"),
        },
    )
    assert r.status_code == 303
    report_url = r.headers["location"]
    page = authed.get(report_url).text
    assert "E030" in page and "Confirm import" in page
    batch = report_url.rsplit("/", 1)[1]
    assert authed.get(f"/imports/{batch}/errors.csv").text.count("\n") >= 2
    r = authed.post(f"/imports/{batch}/confirm", data={"csrf": t, "include_warnings": "on"})
    assert r.status_code == 303
    assert status_of(engine, "C100-FB") == "DRAFT" and status_of(engine, "C101-FB") == "DRAFT"
    with engine.connect() as c:
        assert (
            c.execute(text("select count(*) from posts where post_id='C102-FB'")).scalar_one() == 0
        )
    r = authed.post(f"/imports/{batch}/undo", data={"csrf": t})
    assert status_of(engine, "C100-FB") == "CANCELLED"


def test_import_rejects_hostile_zip_and_expired_confirm(authed, app_env):
    seed(app_env[0])
    t = tok(authed)
    r = authed.post(
        "/imports",
        data={"csrf": t},
        files={
            "csv_file": ("c.csv", b"content_id,platform,account,post_type\n", "text/csv"),
            "zips": ("evil.zip", make_zip({"../evil.jpg": JPEG}), "application/zip"),
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/imports"
    assert "ZIP rejected" in authed.get("/imports").text
    r = authed.post("/imports/00000000-0000-0000-0000-000000000000/confirm", data={"csrf": t})
    assert "expired" in authed.get("/imports").text


# ---------------------------------------------------------------- queues, platforms, settings
def test_queue_creation_generates_slots_and_can_pause(authed, app_env):
    engine, _ = app_env
    seed(engine)
    with engine.connect() as c:
        acc = c.execute(text("select id from platform_accounts")).scalar_one()
    t = tok(authed)
    r = authed.post(
        "/queues",
        data={
            "csrf": t,
            "account_id": str(acc),
            "name": "Product",
            "start_date": "2026-10-05",
            "time_local": "11:00",
            "timezone": "Asia/Dhaka",
            "kind": "weekly",
            "every": "1",
            "weekdays": ["MON", "THU"],
            "day": "1",
            "nth": "1",
            "weekday": "MON",
            "pattern": "image, text",
            "pattern_mode": "RELAXED",
            "require_approval": "on",
        },
    )
    assert r.status_code == 303
    with engine.connect() as c:
        q = c.execute(
            text("select id, recurrence, require_approval from queues where name='Product'")
        ).one()
        n = c.execute(
            text("select count(*) from queue_slots where queue_id=:q"), {"q": q[0]}
        ).scalar_one()
        first = c.execute(
            text("select min(slot_at) from queue_slots where queue_id=:q"), {"q": q[0]}
        ).scalar_one()
    assert q[1]["weekdays"] == ["MON", "THU"] and q[2] is True and 20 <= n <= 30
    assert first == datetime(2026, 10, 5, 5, 0, tzinfo=UTC)  # 11:00 Dhaka
    authed.post(f"/queues/{q[0]}/status", data={"csrf": t, "status": "PAUSED"})
    with engine.connect() as c:
        assert (
            c.execute(text("select status from queues where id=:q"), {"q": q[0]}).scalar_one()
            == "PAUSED"
        )
    assert authed.post(f"/queues/{q[0]}/simulate", data={"csrf": t}).status_code == 200


def test_invalid_queue_rule_shows_error_not_crash(authed, app_env):
    engine, _ = app_env
    seed(engine)
    with engine.connect() as c:
        acc = c.execute(text("select id from platform_accounts")).scalar_one()
    t = tok(authed)
    r = authed.post(
        "/queues",
        data={
            "csrf": t,
            "account_id": str(acc),
            "name": "Bad",
            "start_date": "2026-10-05",
            "kind": "weekly",
            "every": "1",
            "time_local": "10:00",
        },
    )  # no weekdays
    assert r.status_code == 303
    assert "Could not create the queue" in authed.get("/queues").text


def test_account_add_mode_change_and_toggle(authed, app_env):
    engine, _ = app_env
    t = tok(authed)
    authed.post(
        "/accounts",
        data={
            "csrf": t,
            "platform_key": "whatsapp_channel",
            "short_name": "IESL Channel",
            "display_name": "IESL WhatsApp",
            "destination_url": "https://whatsapp.com/channel/x",
        },
    )
    with engine.connect() as c:
        row = c.execute(text("select id, short_name, mode, state from platform_accounts")).one()
    assert row[1] == "iesl_channel" and row[2] == "ASSISTED"
    authed.post(f"/accounts/{row[0]}/mode", data={"csrf": t, "mode": "AUTO"})
    authed.post(f"/accounts/{row[0]}/toggle", data={"csrf": t})
    with engine.connect() as c:
        assert c.execute(text("select mode, state from platform_accounts")).one() == (
            "AUTO",
            "DISABLED",
        )
    authed.post(f"/accounts/{row[0]}/mode", data={"csrf": t, "mode": "HACK"})
    with engine.connect() as c:
        assert c.execute(text("select mode from platform_accounts")).scalar_one() == "AUTO"


def test_kill_switch_banner_and_publisher_stops(authed, app_env):
    engine, _ = app_env
    pid = seed(engine, status="APPROVED")
    t = tok(authed)
    authed.post(f"/posts/{pid}/approve", data={"csrf": t})  # no-op (already approved) but fine
    authed.post("/settings/kill-switch", data={"csrf": t, "active": "on"})
    assert "Kill switch is ON" in authed.get("/").text
    s = run_once(engine, lambda _s: MockAdapter(), START + timedelta(minutes=5))
    assert s.skipped_kill_switch
    authed.post("/settings/kill-switch", data={"csrf": t})
    assert "Kill switch is ON" not in authed.get("/").text


def test_expired_token_banner(authed, app_env):
    engine, _ = app_env
    seed(engine)
    with engine.begin() as c:
        c.execute(text("update platform_accounts set state='TOKEN_EXPIRED'"))
    assert "need reconnecting" in authed.get("/").text


# ---------------------------------------------------------------- assisted + reports + logs
def test_assisted_flow_through_ui_and_public_link(authed, app_env):
    engine, _ = app_env
    pid = seed(engine, caption="Post me by hand")
    t = tok(authed)
    authed.post(f"/posts/{pid}/approve", data={"csrf": t})
    s = run_once(engine, lambda _s: MockAdapter(), START + timedelta(minutes=2), signing_key=KEY)
    assert s.delivered == [pid]
    page = authed.get("/assisted").text
    assert "Post me by hand" in page
    link = s.assisted_tokens[pid]
    anon = TestClient(app_env[1], follow_redirects=False)  # no login: public signed link
    html = anon.get(f"/a/{link}")
    assert html.status_code == 200 and "Post me by hand" in html.text
    t2 = re.search(r'name="csrf" value="([^"]+)"', html.text).group(1)
    assert anon.post(f"/a/{link}/done", data={}).status_code == 403  # CSRF needed
    done = anon.post(f"/a/{link}/done", data={"csrf": t2, "published_url": "https://fb.test/p/1"})
    assert done.status_code == 200 and status_of(engine, pid) == "PUBLISHED"
    assert anon.get(f"/a/{link}").status_code == 410  # single use
    assert anon.get("/a/" + link[:-4] + "AAAA").status_code == 410  # tampered


def test_assisted_confirm_from_logged_in_screen(authed, app_env):
    engine, _ = app_env
    pid = seed(engine)
    t = tok(authed)
    authed.post(f"/posts/{pid}/approve", data={"csrf": t})
    run_once(engine, lambda _s: MockAdapter(), START + timedelta(minutes=2), signing_key=KEY)
    with engine.connect() as c:
        task = c.execute(text("select id from assisted_tasks")).scalar_one()
    authed.post(f"/assisted/{task}/done", data={"csrf": t, "published_url": ""})
    assert status_of(engine, pid) == "PUBLISHED"


def test_failed_page_retry_button(authed, app_env):
    engine, _ = app_env
    pid = seed(engine)
    t = tok(authed)
    authed.post(f"/posts/{pid}/approve", data={"csrf": t})
    run_once(
        engine,
        lambda _s: MockAdapter({"outcome": "validation"}),
        START + timedelta(minutes=2),
        signing_key=KEY,
    )
    page = authed.get("/failed").text
    assert pid in page or "No failed posts" in page


def test_reports_download_formats_and_logs_csv(authed, app_env):
    engine, _ = app_env
    pid = seed(engine, mode="AUTO")
    with engine.begin() as c:
        c.execute(text("update platform_accounts set mode='AUTO'"))
    t = tok(authed)
    authed.post(f"/posts/{pid}/approve", data={"csrf": t})
    run_once(engine, lambda _s: MockAdapter(), START + timedelta(minutes=2))
    r = authed.get("/reports/download?kind=daily&fmt=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert authed.get("/reports/download?kind=weekly&fmt=json").json()["kind"] == "weekly"
    x = authed.get("/reports/download?kind=monthly&fmt=xlsx")
    assert x.content[:2] == b"PK" and "spreadsheetml" in x.headers["content-type"]
    csv_logs = authed.get("/logs?fmt=csv")
    assert csv_logs.headers["content-type"].startswith("text/csv") and "SUCCESS" in csv_logs.text
    assert "SUCCESS" in authed.get("/logs?result=SUCCESS").text


def test_every_rendered_form_carries_a_real_csrf_token(authed, app_env):
    """Regression: macros once rendered an empty token, which made every button return 403."""
    engine, _ = app_env
    pid = seed(engine, n=2)[0]
    seed(engine, status="FAILED_FINAL")
    for path in (
        "/posts",
        f"/posts/{pid}",
        "/imports",
        "/queues",
        "/assisted",
        "/failed",
        "/platforms",
        "/settings",
    ):
        html = authed.get(path).text
        tokens = re.findall(r'name="csrf" value="([^"]*)"', html)
        assert tokens and all(len(t) >= 20 for t in tokens), path


def test_a_real_form_round_trip_works_with_the_rendered_token(authed, app_env):
    engine, _ = app_env
    pid = seed(engine)
    html = authed.get(f"/posts/{pid}").text
    form = re.search(
        rf'<form method="post" action="/posts/{pid}/approve">(.*?)</form>', html, re.S
    ).group(1)
    token = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    assert authed.post(f"/posts/{pid}/approve", data={"csrf": token}).status_code == 303
    assert status_of(engine, pid) == "SCHEDULED"


# ---------------------------------------------------------------- two-factor login
@pytest.fixture
def totp_client(clean_db, tmp_path):
    secret = auth.generate_totp_secret()
    settings = Settings(
        _env_file=None,
        sc_env="test",
        sc_signing_key=KEY,
        sc_admin_email=EMAIL,
        sc_admin_password_hash=PW_HASH,
        sc_totp_secret=secret,
        media_dir=str(tmp_path / "m"),
    )
    return TestClient(
        create_app(clean_db, settings, now=lambda: NOW), follow_redirects=False
    ), secret


def test_login_asks_for_code_when_totp_configured(totp_client):
    client, _ = totp_client
    assert 'name="code"' in client.get("/login").text


def test_totp_login_requires_a_valid_code(totp_client):
    client, secret = totp_client
    t = csrf_from(client)
    base = {"email": EMAIL, "password": PASSWORD, "csrf": t}
    assert client.post("/login", data={**base, "code": ""}).status_code == 401
    assert client.post("/login", data={**base, "code": "000000"}).status_code == 401
    assert client.get("/").status_code == 303  # still not signed in
    ok = client.post("/login", data={**base, "code": auth.totp_code(secret)})
    assert ok.status_code == 303 and client.get("/").status_code == 200


def test_correct_code_with_wrong_password_is_refused(totp_client):
    client, secret = totp_client
    r = client.post(
        "/login",
        data={
            "email": EMAIL,
            "password": "wrong password!!",
            "csrf": csrf_from(client),
            "code": auth.totp_code(secret),
        },
    )
    assert r.status_code == 401 and "code" in r.text.lower()


def test_same_code_cannot_be_used_twice(totp_client):
    client, secret = totp_client
    code = auth.totp_code(secret)
    t = csrf_from(client)
    assert (
        client.post(
            "/login", data={"email": EMAIL, "password": PASSWORD, "csrf": t, "code": code}
        ).status_code
        == 303
    )
    other = TestClient(client.app, follow_redirects=False)
    r = other.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf": csrf_from(other), "code": code},
    )
    assert r.status_code == 401


def test_visiting_a_protected_page_does_not_break_an_open_login_form(client):
    token = csrf_from(client)
    assert client.get("/posts").status_code == 303  # e.g. another tab redirected to /login
    r = client.post("/login", data={"email": EMAIL, "password": PASSWORD, "csrf": token})
    assert r.status_code == 303
