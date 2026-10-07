"""Assisted publishing: signed links, confirmation, reminders (PRD-08)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text

REMINDER_OFFSETS = (timedelta(hours=2), timedelta(hours=6), timedelta(hours=24))
TOKEN_TTL = timedelta(days=7)


class TokenError(Exception):
    """Invalid, expired, tampered or already-used link."""


@dataclass
class Reminder:
    post_id: str
    number: int
    overdue: bool


# ---------------------------------------------------------------- tokens
def _sign(key: str, payload: str) -> str:
    mac = hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def make_token(key: str, task_id: str, expires_at: datetime) -> str:
    if len(key) < 16:
        raise ValueError("signing key too short")
    payload = f"{task_id}.{int(expires_at.timestamp())}"
    return (
        base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _sign(key, payload)
    )


def parse_token(key: str, token: str, now: datetime) -> str:
    """Return the task id, or raise TokenError."""
    try:
        b64, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
        task_id, exp = payload.rsplit(".", 1)
    except Exception as exc:
        raise TokenError("malformed link") from exc
    if not hmac.compare_digest(sig, _sign(key, payload)):
        raise TokenError("invalid signature")
    if now.timestamp() > int(exp):
        raise TokenError("link expired")
    return task_id


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(conn: Connection, task_id: str, key: str, now: datetime) -> str:
    expires = now + TOKEN_TTL
    token = make_token(key, task_id, expires)
    conn.execute(
        text("update assisted_tasks set token_hash=:h, token_expires_at=:e where id=:i"),
        {"h": _hash(token), "e": expires, "i": task_id},
    )
    return token


def rebuild_token(conn: Connection, task_id: str, key: str) -> str | None:
    """The same link again (for reminders): tokens are deterministic for a given expiry."""
    exp = conn.execute(
        text("select token_expires_at from assisted_tasks where id=:i"), {"i": task_id}
    ).scalar()
    return make_token(key, task_id, exp) if exp else None


def _task_for_token(conn: Connection, token: str, key: str, now: datetime) -> dict[str, Any]:
    task_id = parse_token(key, token, now)
    row = (
        conn.execute(text("select * from assisted_tasks where id=:i for update"), {"i": task_id})
        .mappings()
        .first()
    )
    if row is None or row["token_hash"] != _hash(token):
        raise TokenError("link not recognised")
    if row["state"] in ("CONFIRMED", "SKIPPED"):
        raise TokenError("link already used")
    return dict(row)


# ---------------------------------------------------------------- actions
def load_package_data(conn: Connection, token: str, key: str, now: datetime) -> dict[str, Any]:
    """Data for the public confirm page (does not consume the token)."""
    task = _task_for_token(conn, token, key, now)
    post = (
        conn.execute(
            text(
                """select p.post_id, p.caption, p.title, p.link_url, p.hashtags, p.status,
                      a.destination_url, a.display_name
               from posts p join platform_accounts a on a.id = p.account_id where p.post_id=:p"""
            ),
            {"p": task["post_id"]},
        )
        .mappings()
        .one()
    )
    return {"task_id": str(task["id"]), **dict(post)}


def confirm(
    conn: Connection, token: str, key: str, now: datetime, published_url: str | None = None
) -> str:
    """Owner confirms the manual post: PUBLISHED, logged. Returns the post id."""
    task = _task_for_token(conn, token, key, now)
    post_id = str(task["post_id"])
    row = (
        conn.execute(
            text(
                """select p.account_id, p.queue_id, p.attempt_count, a.platform_key, s.id as slot_id,
                      s.slot_at from posts p
               join platform_accounts a on a.id = p.account_id
               left join queue_slots s on s.post_id = p.post_id and s.state='FILLED'
               where p.post_id=:p"""
            ),
            {"p": post_id},
        )
        .mappings()
        .one()
    )
    conn.execute(
        text(
            """update posts set status='PUBLISHED', published_at=:n, published_url=:u,
                   confirmed_by_owner=true where post_id=:p
                   and status in ('AWAITING_CONFIRMATION','OVERDUE')"""
        ),
        {"p": post_id, "n": now, "u": published_url},
    )
    conn.execute(
        text("update queue_slots set state='DONE' where post_id=:p and state='FILLED'"),
        {"p": post_id},
    )
    conn.execute(
        text(
            """update assisted_tasks set state='CONFIRMED', confirmed_at=:n, confirmed_url=:u
               where id=:i"""
        ),
        {"i": task["id"], "n": now, "u": published_url},
    )
    conn.execute(
        text("update platform_accounts set last_publish_at=:n where id=:a"),
        {"n": now, "a": row["account_id"]},
    )
    conn.execute(
        text(
            """insert into publish_attempts (post_id, slot_id, attempt_no, attempt_type, platform_key,
                   account_id, queue_id, scheduled_at, started_at, finished_at, result, published_url)
               values (:p, :s, :no, 'ASSISTED_CONFIRM', :pk, :a, :q, :sched, :n, :n, 'CONFIRMED', :u)"""
        ),
        {
            "p": post_id,
            "s": row["slot_id"],
            "no": int(row["attempt_count"] or 0) + 1,
            "pk": row["platform_key"],
            "a": row["account_id"],
            "q": row["queue_id"],
            "sched": row["slot_at"],
            "n": now,
            "u": published_url,
        },
    )
    conn.execute(
        text(
            "insert into post_audit (post_id, actor, action, reason) values (:p,'owner','CONFIRMED',:u)"
        ),
        {"p": post_id, "u": published_url or ""},
    )
    return post_id


def skip(conn: Connection, token: str, key: str, now: datetime) -> str:
    task = _task_for_token(conn, token, key, now)
    post_id = str(task["post_id"])
    conn.execute(
        text(
            """update posts set status='SKIPPED' where post_id=:p
               and status in ('AWAITING_CONFIRMATION','OVERDUE')"""
        ),
        {"p": post_id},
    )
    conn.execute(
        text("update queue_slots set state='SKIPPED' where post_id=:p and state='FILLED'"),
        {"p": post_id},
    )
    conn.execute(text("update assisted_tasks set state='SKIPPED' where id=:i"), {"i": task["id"]})
    conn.execute(
        text(
            "insert into post_audit (post_id, actor, action) values (:p,'owner','ASSISTED_SKIPPED')"
        ),
        {"p": post_id},
    )
    return post_id


# ---------------------------------------------------------------- reminders
def process_reminders(conn: Connection, now: datetime) -> list[Reminder]:
    """Send due reminders (+2h, +6h, +24h after delivery); then mark the post OVERDUE (AST-05)."""
    rows = (
        conn.execute(
            text(
                """select id, post_id, delivered_at, reminders_sent from assisted_tasks
               where state='DELIVERED' and next_reminder_at is not null and next_reminder_at <= :n
               order by next_reminder_at"""
            ),
            {"n": now},
        )
        .mappings()
        .all()
    )
    out: list[Reminder] = []
    for r in rows:
        number = int(r["reminders_sent"]) + 1
        last = number >= len(REMINDER_OFFSETS)
        nxt = None if last else r["delivered_at"] + REMINDER_OFFSETS[number]
        conn.execute(
            text(
                """update assisted_tasks set reminders_sent=:k, next_reminder_at=:nx,
                       state=case when :last then 'OVERDUE' else state end where id=:i"""
            ),
            {"k": number, "nx": nxt, "last": last, "i": r["id"]},
        )
        if last:
            conn.execute(
                text(
                    "update posts set status='OVERDUE' where post_id=:p and status='AWAITING_CONFIRMATION'"
                ),
                {"p": r["post_id"]},
            )
        out.append(Reminder(str(r["post_id"]), number, last))
    return out


def utcnow() -> datetime:
    return datetime.now(UTC)
