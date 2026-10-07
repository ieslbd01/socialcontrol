"""Review and approval workflow (PRD-04) on top of Postgres."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from socialcontrol.domain.workflow import approval_hash
from socialcontrol.scheduler import queue_service

EDITABLE_FIELDS = ("title", "caption", "link_url", "post_type", "language", "evergreen", "notes")
SCHEDULED_LIKE = ("APPROVED", "SCHEDULED", "QUEUED")


class ReviewError(Exception):
    """A review action is not allowed; the message is shown to the owner."""


@dataclass
class BulkResult:
    approved: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)  # post_id -> reason


# ---------------------------------------------------------------- helpers
def _post(conn: Connection, post_id: str) -> dict[str, Any]:
    row = (
        conn.execute(
            text("select * from posts where post_id=:p and deleted_at is null for update"),
            {"p": post_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ReviewError(f"post {post_id} not found")
    return dict(row)


def _audit(
    conn: Connection,
    post_id: str,
    actor: str,
    action: str,
    field_: str | None = None,
    old: Any = None,
    new: Any = None,
    reason: str = "",
) -> None:
    conn.execute(
        text(
            """insert into post_audit (post_id, actor, action, field, old_value, new_value, reason)
               values (:p, :a, :ac, :f, :o, :n, :r)"""
        ),
        {
            "p": post_id,
            "a": actor,
            "ac": action,
            "f": field_,
            "o": None if old is None else str(old),
            "n": None if new is None else str(new),
            "r": reason,
        },
    )


def media_hashes(conn: Connection, post_id: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            text(
                """select m.sha256 from post_media pm join media m on m.id = pm.media_id
                   where pm.post_id=:p order by pm.sort"""
            ),
            {"p": post_id},
        )
    ]


def compute_hash(conn: Connection, post: dict[str, Any]) -> str:
    return approval_hash(
        {
            "post_type": post["post_type"],
            "language": post["language"],
            "title": post["title"],
            "caption": post["caption"],
            "link_url": post["link_url"],
            "hashtags": list(post["hashtags"] or []),
            "media_sha256": media_hashes(conn, post["post_id"]),
            "account_id": str(post["account_id"]),
        }
    )


def validate_for_approval(conn: Connection, post: dict[str, Any]) -> list[str]:
    """Blocking problems (PRD-04: a post with an ERROR cannot be approved)."""
    problems: list[str] = []
    cap = (
        conn.execute(
            text(
                """select c.* from platform_capabilities c
               join platform_accounts a on a.platform_key = c.platform_key
               where a.id=:a and c.post_type=:t"""
            ),
            {"a": post["account_id"], "t": post["post_type"]},
        )
        .mappings()
        .first()
    )
    if cap is None:
        return [f"post type {post['post_type']!r} is not supported on this channel"]
    text_len = len((post["caption"] or "").replace("{link}", post["link_url"] or ""))
    if cap["max_caption_chars"] and text_len > cap["max_caption_chars"]:
        problems.append(f"caption is {text_len} characters; limit {cap['max_caption_chars']}")
    if cap["max_hashtags"] and len(post["hashtags"] or []) > cap["max_hashtags"]:
        problems.append(f"more than {cap['max_hashtags']} hashtags")
    if cap["requires_media"] and not media_hashes(conn, post["post_id"]):
        problems.append("media is required for this post type")
    if post["post_type"] in ("text", "link", "text_image") and not (post["caption"] or "").strip():
        problems.append("caption is required")
    if cap["platform_key"] == "youtube" and not (post["title"] or "").strip():
        problems.append("YouTube needs a title")
    return problems


# ---------------------------------------------------------------- actions
def submit_for_review(conn: Connection, post_id: str, actor: str = "owner") -> None:
    p = _post(conn, post_id)
    if p["status"] != "DRAFT":
        raise ReviewError(f"only DRAFT posts can be submitted (this one is {p['status']})")
    conn.execute(text("update posts set status='IN_REVIEW' where post_id=:p"), {"p": post_id})
    _audit(conn, post_id, actor, "SUBMITTED")


def approve(
    conn: Connection, post_id: str, now: datetime, actor: str = "owner", allocate: bool = True
) -> None:
    p = _post(conn, post_id)
    if p["status"] not in ("DRAFT", "IN_REVIEW"):
        raise ReviewError(f"cannot approve a post that is {p['status']}")
    problems = validate_for_approval(conn, p)
    if problems:
        raise ReviewError("; ".join(problems))
    h = compute_hash(conn, p)
    conn.execute(
        text(
            "update posts set status='APPROVED', approved_at=:n, approved_hash=:h where post_id=:p"
        ),
        {"n": now, "h": h, "p": post_id},
    )
    _audit(conn, post_id, actor, "APPROVED", new=h[:12])
    if allocate and p["queue_id"]:
        queue_service.allocate_queue(conn, p["queue_id"], now)


def reject(conn: Connection, post_id: str, comment: str, actor: str = "owner") -> None:
    if not comment.strip():
        raise ReviewError("a rejection needs a comment")
    p = _post(conn, post_id)
    if p["status"] not in ("IN_REVIEW", "DRAFT", "APPROVED"):
        raise ReviewError(f"cannot reject a post that is {p['status']}")
    if p["status"] == "APPROVED":
        raise ReviewError("edit or cancel an approved post instead of rejecting it")
    conn.execute(
        text("update posts set status='DRAFT', notes=:c where post_id=:p"),
        {"c": f"REJECTED: {comment}", "p": post_id},
    )
    _audit(conn, post_id, actor, "REJECTED", reason=comment)


def edit_post(
    conn: Connection, post_id: str, changes: dict[str, Any], now: datetime, actor: str = "owner"
) -> None:
    """Edit content. Approved/scheduled posts return to IN_REVIEW and free their slot (REV-05)."""
    p = _post(conn, post_id)
    unknown = set(changes) - set(EDITABLE_FIELDS) - {"hashtags"}
    if unknown:
        raise ReviewError(f"cannot edit: {', '.join(sorted(unknown))}")
    status = p["status"]
    if status in ("PUBLISHING", "AWAITING_CONFIRMATION", "PUBLISHED"):
        raise ReviewError("a published or in-flight post is read-only; clone it instead")

    real = {
        k: v
        for k, v in changes.items()
        if (list(v) if k == "hashtags" else v) != (list(p[k] or []) if k == "hashtags" else p[k])
    }
    if not real:
        return
    if status in SCHEDULED_LIKE and p["queue_id"]:
        queue_service.release_post(conn, p["queue_id"], post_id, now, "IN_REVIEW")
    elif status in SCHEDULED_LIKE:
        conn.execute(
            text("update posts set status='IN_REVIEW', scheduled_at=null where post_id=:p"),
            {"p": post_id},
        )
    sets = ", ".join(f"{k}=:{k}" for k in real)
    conn.execute(
        text(f"update posts set {sets}, approved_at=null, approved_hash=null where post_id=:p"),
        {**real, "p": post_id},
    )
    for k, v in real.items():
        _audit(conn, post_id, actor, "EDITED", k, p[k], v)
    if status in SCHEDULED_LIKE:
        _audit(conn, post_id, actor, "RETURNED_TO_REVIEW", reason="content changed after approval")


def cancel(
    conn: Connection, post_id: str, now: datetime, actor: str = "owner", skip: bool = False
) -> None:
    p = _post(conn, post_id)
    status = "SKIPPED" if skip else "CANCELLED"
    if p["status"] in ("PUBLISHING", "PUBLISHED"):
        raise ReviewError(f"cannot {status.lower()} a post that is {p['status']}")
    if p["queue_id"] and p["status"] in SCHEDULED_LIKE:
        queue_service.release_post(conn, p["queue_id"], post_id, now, status)
    else:
        conn.execute(
            text("update posts set status=:s, scheduled_at=null where post_id=:p"),
            {"s": status, "p": post_id},
        )
    _audit(conn, post_id, actor, status)


def retry_now(conn: Connection, post_id: str, now: datetime, actor: str = "owner") -> None:
    """Manual retry of a failed post: counter reset, picked up by the next publisher run."""
    p = _post(conn, post_id)
    if p["status"] not in ("FAILED_FINAL", "NEEDS_ATTENTION", "OVERDUE", "FAILED"):
        raise ReviewError(f"nothing to retry: post is {p['status']}")
    has_slot = conn.execute(
        text("select 1 from queue_slots where post_id=:p and state='FILLED'"), {"p": post_id}
    ).first()
    if not has_slot:
        raise ReviewError("post has no slot; approve it into a queue first")
    conn.execute(
        text(
            """update posts set status='RETRYING', attempt_count=0, next_retry_at=:n,
                   locked_by=null, locked_at=null where post_id=:p"""
        ),
        {"n": now, "p": post_id},
    )
    _audit(conn, post_id, actor, "MANUAL_RETRY")


def bulk_approve(
    conn: Connection,
    post_ids: Iterable[str],
    now: datetime,
    actor: str = "owner",
    allocate: bool = True,
) -> BulkResult:
    """Approve many posts; those with blocking problems are excluded with a reason (REV-04)."""
    result = BulkResult()
    queues: set[Any] = set()
    for pid in post_ids:
        try:
            with conn.begin_nested():
                approve(conn, pid, now, actor, allocate=False)
            result.approved.append(pid)
            q = conn.execute(
                text("select queue_id from posts where post_id=:p"), {"p": pid}
            ).scalar_one()
            if q:
                queues.add(q)
        except ReviewError as exc:
            result.excluded[pid] = str(exc)
    if allocate:
        for q in queues:
            queue_service.allocate_queue(conn, q, now)
    return result


def set_kill_switch(conn: Connection, active: bool, reason: str = "", actor: str = "owner") -> None:
    conn.execute(
        text(
            """insert into settings (key, value) values ('kill_switch', to_jsonb(cast(:v as boolean)))
               on conflict (key) do update set value = excluded.value, updated_at = now()"""
        ),
        {"v": active},
    )
    conn.execute(
        text(
            "insert into security_events (kind, detail) values ('KILL_SWITCH', cast(:d as jsonb))"
        ),
        {"d": f'{{"active": {str(active).lower()}, "actor": "{actor}"}}'},
    )
    _ = reason
