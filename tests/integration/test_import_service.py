"""Import service tests (PRD-03 / T-IMP-*): dry run, confirm, idempotency, re-import rules, undo."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from socialcontrol.imports import import_service as svc
from socialcontrol.scheduler import queue_service as qs

pytestmark = pytest.mark.integration

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
JPEG2 = b"\xff\xd8\xff\xe1" + b"\x01" * 64
HEADER = "content_id,platform,account,queue,post_type,language,title,caption,link,media_file,hashtags,evergreen,approved\n"
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


class FakeStorage:
    def __init__(self):
        self.puts: list[str] = []

    def put(self, key, data, mime):
        self.puts.append(key)
        return f"https://store.test/{key}"


def zip_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


@pytest.fixture
def env(clean_db):
    with clean_db.begin() as c:
        for pk, short in (("facebook_page", "iesl_page"), ("linkedin_company", "iesl_company")):
            acc = c.execute(
                text(
                    """insert into platform_accounts (platform_key, short_name, display_name, mode)
                       values (:p, :s, :s, 'ASSISTED') returning id"""
                ),
                {"p": pk, "s": short},
            ).scalar_one()
            c.execute(
                text(
                    """insert into queues (account_id, name, start_at, recurrence, is_default)
                       values (:a, :n, :t, cast(:r as jsonb), true)"""
                ),
                {
                    "a": acc,
                    "n": "main" if pk == "facebook_page" else "technical",
                    "t": datetime(2026, 10, 1, 4, tzinfo=UTC),
                    "r": '{"type": "interval_days", "every": 7}',
                },
            )
    return clean_db


def do_import(engine, csv_body, zips=(), storage=None, **confirm_kw):
    storage = storage or FakeStorage()
    with engine.begin() as c:
        run, files = svc.dry_run(c, (HEADER + csv_body).encode(), list(zips))
    with engine.begin() as c:
        counts = svc.confirm(c, run, files, storage, **confirm_kw)
    return run, counts, storage


def q(engine, sql, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).all()


ROWS = (
    "C001,FB,iesl_page,main,text_image,en,,Hello {link},https://ieslbd.com/a,,#cal,no,no\n"
    "C001,LI,iesl_company,technical,text_image,en,,LinkedIn text,https://ieslbd.com/a,,#cal,no,no\n"
    "C002,FB,iesl_page,main,text_image,bn,,তাপমাত্রা ক্যালিব্রেশন 🌡️,,C002.jpg,#cal,yes,no\n"
)


def test_dry_run_reports_without_creating_posts(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2, "extra.jpg": JPEG})
    with env.begin() as c:
        run, _ = svc.dry_run(c, (HEADER + ROWS).encode(), [z])
    assert run.summary["error"] == 0 and run.summary["valid"] + run.summary["warning"] == 3
    assert any(i.code == "W001" and "extra.jpg" in i.message for i in run.issues)
    assert q(env, "select count(*) from posts")[0][0] == 0
    assert q(env, "select status from import_batches")[0][0] == "DRY_RUN"


def test_confirm_creates_content_posts_media_and_shares_media_by_content_id(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2})
    run, counts, storage = do_import(env, ROWS, [z])
    assert counts == {"new": 3, "updated": 0, "unchanged": 0, "skipped": 0}
    assert q(env, "select count(*) from content_items")[0][0] == 2
    posts = {
        r[0]: r for r in q(env, "select post_id, status, language, caption, queue_id from posts")
    }
    assert set(posts) == {"C001-FB", "C001-LI", "C002-FB"}
    assert posts["C001-FB"][1] == "DRAFT" and posts["C002-FB"][2] == "bn"
    assert posts["C002-FB"][3] == "তাপমাত্রা ক্যালিব্রেশন 🌡️"
    assert posts["C001-FB"][4] is not None  # default queue resolved
    # C001.jpg is shared by FB and LI but stored once
    shared = q(env, "select count(*) from post_media where post_id in ('C001-FB','C001-LI')")[0][0]
    assert shared == 2 and len(storage.puts) == 2
    assert q(env, "select status from import_batches")[0][0] == "CONFIRMED"


def test_reimport_identical_file_changes_nothing(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2})
    do_import(env, ROWS, [z])
    before = q(env, "select post_id, updated_at from posts order by post_id")
    _, counts, storage = do_import(env, ROWS, [z])
    assert counts == {"new": 0, "updated": 0, "unchanged": 3, "skipped": 0}
    assert storage.puts == []  # media already stored
    assert q(env, "select post_id, updated_at from posts order by post_id") == before


def test_reimport_updates_draft_in_place(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2})
    do_import(env, ROWS, [z])
    changed = ROWS.replace("LinkedIn text", "LinkedIn text v2")
    _, counts, _ = do_import(env, changed, [z])
    assert counts["updated"] == 1 and counts["unchanged"] == 2
    assert q(env, "select caption from posts where post_id='C001-LI'")[0][0] == "LinkedIn text v2"


def test_reimport_of_approved_post_needs_overwrite_and_returns_to_review(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2})
    do_import(env, ROWS, [z])
    with env.begin() as c:
        c.execute(
            text("update posts set status='APPROVED', approved_at=now() where post_id='C001-FB'")
        )
    changed = ROWS.replace("Hello {link}", "Hello again {link}")
    _, counts, _ = do_import(env, changed, [z])
    assert counts["skipped"] == 1
    assert q(env, "select status from posts where post_id='C001-FB'")[0][0] == "APPROVED"
    _, counts, _ = do_import(env, changed, [z], overwrite=True)
    assert counts["updated"] == 1
    assert q(env, "select status, approved_hash from posts where post_id='C001-FB'")[0] == (
        "IN_REVIEW",
        None,
    )


def test_published_post_is_blocked_with_e070(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2})
    do_import(env, ROWS, [z])
    with env.begin() as c:
        c.execute(
            text("update posts set status='PUBLISHED', approved_at=now() where post_id='C001-FB'")
        )
    changed = ROWS.replace("Hello {link}", "Hacked {link}")
    with env.begin() as c:
        run, _ = svc.dry_run(c, (HEADER + changed).encode(), [z])
    assert any(i.code == "E070" for i in run.issues)
    do_import(env, changed, [z])
    assert q(env, "select caption from posts where post_id='C001-FB'")[0][0] == "Hello {link}"


def test_missing_media_excludes_only_that_row(env):
    z = zip_of({"C001.jpg": JPEG})  # C002.jpg missing
    run, counts, _ = do_import(env, ROWS, [z])
    codes = {(i.row, i.code) for i in run.issues if i.level.value == "ERROR"}
    assert (3, "E041") in codes
    assert counts["new"] == 2
    assert {r[0] for r in q(env, "select post_id from posts")} == {"C001-FB", "C001-LI"}


def test_corrupt_zip_entry_reported_but_valid_files_import(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": b"not an image"})
    run, counts, _ = do_import(env, ROWS, [z])
    assert any(i.code == "E043" and "C002.jpg" in i.message for i in run.issues)
    assert counts["new"] == 2  # C002 has no usable media -> E041/E040


def test_approved_yes_gets_hash_and_can_be_scheduled(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2})
    rows = ROWS.replace(",no,no\nC001,LI", ",no,yes\nC001,LI", 1)  # C001-FB approved
    do_import(env, rows, [z])
    st = q(
        env,
        "select status, approved_hash, approved_at is not null from posts where post_id='C001-FB'",
    )[0]
    assert st[0] == "APPROVED" and st[1] and st[2]
    with env.begin() as c:
        qid = c.execute(text("select queue_id from posts where post_id='C001-FB'")).scalar_one()
        s = qs.allocate_queue(c, qid, NOW)
    assert s.scheduled == ["C001-FB"]
    assert q(env, "select status from posts where post_id='C001-FB'")[0][0] == "SCHEDULED"


def test_warnings_can_be_excluded_on_confirm(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2})
    rows = ROWS.replace("#cal,no,no\nC001,LI", "#cal,no,no\nC001,LI", 1).replace(
        ",#cal,yes,no", ",,yes,no"
    )  # C002 -> no hashtags (W003)
    _, counts, _ = do_import(env, rows, [z], include_warnings=False)
    assert counts["skipped"] == 1 and counts["new"] == 2


def test_undo_removes_unpublished_posts_and_frees_slots(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2})
    rows = ROWS.replace(",no,no\nC001,LI", ",no,yes\nC001,LI", 1)
    run, _, _ = do_import(env, rows, [z])
    with env.begin() as c:
        qid = c.execute(text("select queue_id from posts where post_id='C001-FB'")).scalar_one()
        qs.allocate_queue(c, qid, NOW)
        c.execute(
            text("update posts set status='PUBLISHED', approved_at=now() where post_id='C001-LI'")
        )
    with env.begin() as c:
        res = svc.undo_batch(c, run.batch_id)
    assert res["undone"] == 2 and res["kept"] == 1  # published one stays
    assert q(env, "select status from posts where post_id='C001-FB'")[0][0] == "CANCELLED"
    assert q(env, "select count(*) from queue_slots where post_id='C001-FB'")[0][0] == 0
    assert q(env, "select status from import_batches where id=:b", b=run.batch_id)[0][0] == "UNDONE"


def test_reimport_after_undo_restores_posts(env):
    z = zip_of({"C001.jpg": JPEG, "C002.jpg": JPEG2})
    run, _, _ = do_import(env, ROWS, [z])
    with env.begin() as c:
        svc.undo_batch(c, run.batch_id)
    _, counts, _ = do_import(env, ROWS, [z])
    assert counts["updated"] == 3
    assert (
        q(env, "select count(*) from posts where deleted_at is null and status='DRAFT'")[0][0] == 3
    )
