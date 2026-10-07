"""Publisher engine (PRD-07, TDD-03 section 10).

One ``run_once`` = one publisher cycle. It is stateless and idempotent: all state
lives in Postgres. Platform logic is reached only through adapters.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, text

from socialcontrol.assisted.service import issue_token
from socialcontrol.domain.enums import FailureClass, PostStatus
from socialcontrol.domain.workflow import approval_hash
from socialcontrol.platforms.base import (
    AdapterError,
    MediaView,
    PlatformAdapter,
    PostView,
    PublishContext,
)
from socialcontrol.retry.policy import RetryAction, RetryPolicy, decide
from socialcontrol.scheduler import queue_service

AdapterFactory = Callable[[dict[str, Any]], PlatformAdapter]

DEFAULT_GRACE_WINDOW = timedelta(hours=6)
DEFAULT_BATCH_LIMIT = 10
STUCK_AFTER = timedelta(minutes=20)


@dataclass
class RunSummary:
    run_id: str
    published: list[str] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)  # assisted packages
    failed: list[str] = field(default_factory=list)
    retry_scheduled: list[str] = field(default_factory=list)
    overdue: list[str] = field(default_factory=list)
    skipped_kill_switch: bool = False
    needs_attention: list[str] = field(default_factory=list)
    blocked: list[tuple[str, str]] = field(default_factory=list)  # (post_id, reason)
    notifications: list[tuple[str, str]] = field(default_factory=list)  # (event, post_id)
    assisted_tokens: dict[str, str] = field(default_factory=dict)  # post_id -> signed token


# ---------------------------------------------------------------- helpers
def _get_setting(conn: Connection, key: str, default: object) -> object:
    row = conn.execute(text("select value from settings where key = :k"), {"k": key}).first()
    return row[0] if row else default


def kill_switch_on(conn: Connection) -> bool:
    return bool(_get_setting(conn, "kill_switch", False))


def _audit(
    conn: Connection, post_id: str, action: str, reason: str = "", actor: str = "system"
) -> None:
    conn.execute(
        text("insert into post_audit (post_id, actor, action, reason) values (:p, :a, :ac, :r)"),
        {"p": post_id, "a": actor, "ac": action, "r": reason},
    )


def _build_post_view(conn: Connection, row: dict[str, Any]) -> tuple[PostView, str]:
    media_rows = conn.execute(
        text(
            """select m.sha256, m.mime, m.bytes, m.public_url, m.storage_key, m.width, m.height,
                      m.duration_s
               from post_media pm join media m on m.id = pm.media_id
               where pm.post_id = :p order by pm.sort"""
        ),
        {"p": row["post_id"]},
    ).mappings()
    media = tuple(
        MediaView(
            url=r["public_url"] or r["storage_key"],
            mime=r["mime"],
            bytes=r["bytes"],
            sha256=r["sha256"],
            width=r["width"],
            height=r["height"],
            duration_s=float(r["duration_s"]) if r["duration_s"] is not None else None,
        )
        for r in media_rows
    )
    view = PostView(
        post_id=str(row["post_id"]),
        post_type=str(row["post_type"]),
        language=str(row["language"]),
        caption=str(row["caption"] or ""),
        title=row["title"],
        link_url=row["link_url"],
        hashtags=tuple(str(h) for h in (row["hashtags"] or ())),
        media=media,
    )
    hash_now = approval_hash(
        {
            "post_type": view.post_type,
            "language": view.language,
            "title": view.title,
            "caption": row["caption"],
            "link_url": view.link_url,
            "hashtags": list(view.hashtags),
            "media_sha256": [m.sha256 for m in media],
            "account_id": str(row["account_id"]),
        }
    )
    return view, hash_now


def _attempt(
    conn: Connection,
    row: dict[str, Any],
    run_id: str,
    kind: str,
    result: str,
    started: datetime,
    finished: datetime,
    failure: FailureClass | None = None,
    error_code: str = "",
    error_message: str = "",
    platform_post_id: str | None = None,
    url: str | None = None,
    key: str = "",
    platform_key: str = "",
) -> None:
    conn.execute(
        text(
            """insert into publish_attempts
               (post_id, run_id, attempt_no, attempt_type, platform_key, account_id, queue_id,
                scheduled_at, started_at, finished_at, result, failure_class, error_code,
                error_message, platform_post_id, published_url, idempotency_key, response_meta)
               values (:post_id, :run, :no, :kind, :pk, :acc, :q, :sched, :s, :f, :res, :fc, :ec,
                       :em, :ppid, :url, :key, cast(:meta as jsonb))"""
        ),
        {
            "post_id": row["post_id"],
            "run": run_id,
            "no": int(row["attempt_count"] or 0) + 1,
            "kind": kind,
            "pk": platform_key,
            "acc": row["account_id"],
            "q": row["queue_id"],
            "sched": row["slot_at"],
            "s": started,
            "f": finished,
            "res": result,
            "fc": failure.value if failure else None,
            "ec": error_code or None,
            "em": error_message[:1000] or None,
            "ppid": platform_post_id,
            "url": url,
            "key": key or None,
            "meta": json.dumps({}),
        },
    )


# ---------------------------------------------------------------- the cycle
def run_once(
    engine: Engine,
    adapters: AdapterFactory,
    now: datetime,
    run_id: str | None = None,
    policy: RetryPolicy | None = None,
    batch_limit: int | None = None,
    grace_window: timedelta | None = None,
    maintain: bool = True,
    signing_key: str = "",
) -> RunSummary:
    """Process one publisher cycle at time ``now`` (injectable clock)."""
    run_id = run_id or uuid.uuid4().hex[:12]
    policy = policy or RetryPolicy()
    limit = batch_limit or DEFAULT_BATCH_LIMIT
    grace = grace_window or DEFAULT_GRACE_WINDOW
    summary = RunSummary(run_id)

    with engine.begin() as conn:
        conn.execute(
            text("insert into job_runs (job, run_id, started_at) values ('publisher', :r, :n)"),
            {"r": run_id, "n": now},
        )
        if kill_switch_on(conn):
            summary.skipped_kill_switch = True
            conn.execute(
                text(
                    "update job_runs set finished_at=:n, ok=true, summary=cast(:s as jsonb) where run_id=:r"
                ),
                {"n": now, "r": run_id, "s": json.dumps({"kill_switch": True})},
            )
            return summary

    if maintain:
        _maintain_queues(engine, now, summary)

    # --- select due work (no locks held while calling adapters)
    with engine.connect() as conn:
        due = (
            conn.execute(
                text(
                    """select p.post_id, p.status, p.attempt_count, p.next_retry_at, p.account_id,
                          p.queue_id, p.post_type, p.language, p.title, p.caption, p.link_url,
                          p.hashtags, p.approved_hash, s.id as slot_id, s.slot_at,
                          a.mode, a.state as account_state, a.test_mode, a.settings,
                          a.destination_url, pl.key as platform_key, q.status as queue_status
                   from posts p
                   join queue_slots s on s.post_id = p.post_id and s.state = 'FILLED'
                   join queues q on q.id = s.queue_id
                   join platform_accounts a on a.id = p.account_id
                   join platforms pl on pl.key = a.platform_key
                   where p.status in ('SCHEDULED','RETRYING')
                     and s.slot_at <= :now
                     and (p.next_retry_at is null or p.next_retry_at <= :now)
                     and q.status = 'ACTIVE'
                     and a.state <> 'DISABLED'
                   order by q.priority, s.slot_at
                   limit :lim"""
                ),
                {"now": now, "lim": limit},
            )
            .mappings()
            .all()
        )

    for row in (dict(r) for r in due):
        _process(engine, row, adapters, now, run_id, policy, grace, summary, signing_key)

    _recover_stuck(engine, adapters, now, run_id, summary)

    with engine.begin() as conn:
        conn.execute(
            text(
                """update job_runs set finished_at=:n, ok=true, summary=cast(:s as jsonb)
                   where run_id=:r"""
            ),
            {
                "n": now,
                "r": run_id,
                "s": json.dumps(
                    {
                        "published": summary.published,
                        "delivered": summary.delivered,
                        "failed": summary.failed,
                        "retry_scheduled": summary.retry_scheduled,
                        "overdue": summary.overdue,
                    }
                ),
            },
        )
    return summary


def _maintain_queues(engine: Engine, now: datetime, summary: RunSummary) -> None:
    """Slots, allocation, empty-slot handling and runway alerts for every active queue."""
    with engine.connect() as conn:
        queue_ids = [
            r[0] for r in conn.execute(text("select id from queues where status='ACTIVE'"))
        ]
    for qid in queue_ids:
        with engine.begin() as conn:
            queue_service.allocate_queue(conn, qid, now)
            result = queue_service.fill_empty_slots(conn, qid, now)
        for slot_id in result["empty"]:
            summary.notifications.append(("EMPTY_SLOT", slot_id))
    with engine.connect() as conn:
        for qid, _days, _threshold in queue_service.queues_low_on_runway(conn, now):
            summary.notifications.append(("RUNWAY_LOW", qid))


def _claim(conn: Connection, post_id: str, run_id: str, now: datetime) -> bool:
    """Atomic SCHEDULED/RETRYING -> PUBLISHING (PUB-02). Returns False if another run won."""
    res = conn.execute(
        text(
            """update posts set status='PUBLISHING', locked_by=:r, locked_at=:n
               where post_id=:p and status in ('SCHEDULED','RETRYING') returning post_id"""
        ),
        {"p": post_id, "r": run_id, "n": now},
    ).first()
    return res is not None


def _process(
    engine: Engine,
    row: dict[str, Any],
    adapters: AdapterFactory,
    now: datetime,
    run_id: str,
    policy: RetryPolicy,
    grace: timedelta,
    summary: RunSummary,
    signing_key: str = "",
) -> None:
    post_id = str(row["post_id"])
    slot_at: datetime = row["slot_at"]
    mode = str(row["mode"])

    # grace window: never silently publish stale content (PUB-07)
    # a retry (scheduled or manual) restarts the clock: age is measured from the later of the two
    retry_at: datetime | None = row.get("next_retry_at")
    reference = max(slot_at, retry_at) if retry_at else slot_at
    if now - reference > grace:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "update posts set status='OVERDUE' where post_id=:p and status in ('SCHEDULED','RETRYING')"
                ),
                {"p": post_id},
            )
            _audit(conn, post_id, "OVERDUE", f"slot {slot_at.isoformat()} older than grace window")
        summary.overdue.append(post_id)
        summary.notifications.append(("POST_OVERDUE", post_id))
        return

    # AUTO needs a usable connection; otherwise hold the post (no retry burn)
    if mode == "AUTO" and row["account_state"] != "CONNECTED":
        summary.blocked.append((post_id, f"account {row['account_state']}"))
        return

    with engine.begin() as conn:
        if not _claim(conn, post_id, run_id, now):
            return  # lost the race: another run owns it
        view, hash_now = _build_post_view(conn, row)
        _audit(conn, post_id, "CLAIMED", run_id)

    started = now
    # approval hash: detect out-of-band changes (REV-06)
    if row["approved_hash"] and row["approved_hash"] != hash_now:
        _fail_blocking(
            engine,
            row,
            run_id,
            summary,
            started,
            "HASH_MISMATCH",
            "post changed after approval",
            FailureClass.VALIDATION,
        )
        return

    factory_settings = dict(row["settings"] or {})
    factory_settings["_platform_key"] = row["platform_key"]
    factory_settings["_mode"] = mode
    factory_settings["destination_url"] = row.get("destination_url")
    adapter = adapters(factory_settings)

    issues = [i for i in adapter.validate_post(view) if i.blocking]
    if issues:
        _fail_blocking(
            engine,
            row,
            run_id,
            summary,
            started,
            issues[0].code,
            issues[0].message,
            FailureClass.VALIDATION,
        )
        return

    key = f"{post_id}:{row['slot_id']}"

    if mode == "ASSISTED" or not adapter.supports_auto:
        _deliver_assisted(engine, row, adapter, view, run_id, summary, now, key, signing_key)
        return

    ctx = PublishContext(
        idempotency_key=key,
        attempt_no=int(row["attempt_count"] or 0) + 1,
        run_id=run_id,
        dry_run=bool(row["test_mode"]),
    )
    try:
        result = adapter.publish(view, ctx)
    except AdapterError as exc:
        _handle_failure(
            engine,
            row,
            run_id,
            summary,
            started,
            now,
            exc.failure_class,
            exc.code,
            str(exc),
            exc.retry_after,
            policy,
        )
        return
    except Exception as exc:  # unexpected: classify through the adapter, default UNKNOWN
        _handle_failure(
            engine,
            row,
            run_id,
            summary,
            started,
            now,
            adapter.map_error(exc),
            type(exc).__name__,
            str(exc),
            None,
            policy,
        )
        return

    with engine.begin() as conn:
        conn.execute(
            text(
                """update posts set status='PUBLISHED', platform_post_id=:pp, published_url=:u,
                       published_at=:n, locked_by=null, locked_at=null,
                       attempt_count=attempt_count+1, next_retry_at=null
                   where post_id=:p"""
            ),
            {"pp": result.platform_post_id, "u": result.url, "n": now, "p": post_id},
        )
        conn.execute(text("update queue_slots set state='DONE' where id=:s"), {"s": row["slot_id"]})
        conn.execute(
            text("update platform_accounts set last_publish_at=:n, last_error=null where id=:a"),
            {"n": now, "a": row["account_id"]},
        )
        _attempt(
            conn,
            row,
            run_id,
            "AUTO_PUBLISH",
            "SUCCESS",
            started,
            now,
            platform_post_id=result.platform_post_id,
            url=result.url,
            key=key,
            platform_key=str(row["platform_key"]),
        )
        _audit(conn, post_id, "PUBLISHED", result.platform_post_id)
    summary.published.append(post_id)
    summary.notifications.append(("PUBLISH_SUCCESS", post_id))
    if result.warnings:
        summary.needs_attention.append(post_id)
        summary.notifications.append(("PUBLISH_WARNING", post_id))


def _deliver_assisted(
    engine: Engine,
    row: dict[str, Any],
    adapter: PlatformAdapter,
    view: PostView,
    run_id: str,
    summary: RunSummary,
    now: datetime,
    key: str,
    signing_key: str = "",
) -> None:
    post_id = str(row["post_id"])
    package = adapter.build_assisted_package(view)  # noqa: F841 - delivered by the notifier layer
    with engine.begin() as conn:
        conn.execute(
            text(
                """update posts set status='AWAITING_CONFIRMATION', locked_by=null, locked_at=null,
                       attempt_count=attempt_count+1 where post_id=:p"""
            ),
            {"p": post_id},
        )
        task_id = conn.execute(
            text(
                """insert into assisted_tasks (post_id, delivered_at, state, next_reminder_at)
                   values (:p, :n, 'DELIVERED', :r) returning id"""
            ),
            {"p": post_id, "n": now, "r": now + timedelta(hours=2)},
        ).scalar_one()
        if signing_key:
            summary.assisted_tokens[post_id] = issue_token(conn, str(task_id), signing_key, now)
        _attempt(
            conn,
            row,
            run_id,
            "ASSISTED_DELIVERY",
            "DELIVERED",
            now,
            now,
            key=key,
            platform_key=str(row["platform_key"]),
        )
        _audit(conn, post_id, "ASSISTED_DELIVERED", package.destination_url or "")
    summary.delivered.append(post_id)
    summary.notifications.append(("ASSISTED_DUE", post_id))


def _fail_blocking(
    engine: Engine,
    row: dict[str, Any],
    run_id: str,
    summary: RunSummary,
    started: datetime,
    code: str,
    message: str,
    failure: FailureClass,
) -> None:
    post_id = str(row["post_id"])
    with engine.begin() as conn:
        conn.execute(
            text(
                """update posts set status='FAILED_FINAL', locked_by=null, locked_at=null,
                       attempt_count=attempt_count+1 where post_id=:p"""
            ),
            {"p": post_id},
        )
        _attempt(
            conn,
            row,
            run_id,
            "AUTO_PUBLISH",
            "FAILED",
            started,
            started,
            failure,
            code,
            message,
            platform_key=str(row["platform_key"]),
        )
        _audit(conn, post_id, "FAILED_FINAL", f"{code}: {message}")
    summary.failed.append(post_id)
    summary.notifications.append(("FINAL_FAILURE", post_id))


def _handle_failure(
    engine: Engine,
    row: dict[str, Any],
    run_id: str,
    summary: RunSummary,
    started: datetime,
    now: datetime,
    failure: FailureClass,
    code: str,
    message: str,
    retry_after: timedelta | None,
    policy: RetryPolicy,
) -> None:
    post_id = str(row["post_id"])
    retries_done = int(row["attempt_count"] or 0)
    decision = decide(failure, retries_done, now, policy, retry_after)
    with engine.begin() as conn:
        if decision.action == RetryAction.RETRY:
            conn.execute(
                text(
                    """update posts set status='RETRYING', locked_by=null, locked_at=null,
                           attempt_count=attempt_count+1, next_retry_at=:nr where post_id=:p"""
                ),
                {"p": post_id, "nr": decision.next_retry_at},
            )
            summary.retry_scheduled.append(post_id)
        elif decision.action == RetryAction.PAUSE_ACCOUNT:
            # post goes back to SCHEDULED so it resumes after reconnect (within grace window)
            conn.execute(
                text(
                    """update posts set status='SCHEDULED', locked_by=null, locked_at=null,
                           attempt_count=attempt_count+1 where post_id=:p"""
                ),
                {"p": post_id},
            )
            conn.execute(
                text(
                    "update platform_accounts set state='TOKEN_EXPIRED', last_error=:e where id=:a"
                ),
                {"a": row["account_id"], "e": message[:500]},
            )
            summary.blocked.append((post_id, "AUTH: account paused"))
            summary.notifications.append(("TOKEN_EXPIRED", post_id))
        else:
            conn.execute(
                text(
                    """update posts set status='FAILED_FINAL', locked_by=null, locked_at=null,
                           attempt_count=attempt_count+1 where post_id=:p"""
                ),
                {"p": post_id},
            )
            summary.failed.append(post_id)
            summary.notifications.append(("FINAL_FAILURE", post_id))
        _attempt(
            conn,
            row,
            run_id,
            "AUTO_PUBLISH",
            "FAILED",
            started,
            now,
            failure,
            code,
            message,
            key=f"{post_id}:{row['slot_id']}",
            platform_key=str(row["platform_key"]),
        )
        _audit(conn, post_id, decision.action.value, f"{failure.value}: {message}")


def _recover_stuck(
    engine: Engine, adapters: AdapterFactory, now: datetime, run_id: str, summary: RunSummary
) -> None:
    """PUBLISHING longer than the timeout is never re-published blindly (PUB-09)."""
    stuck_after = STUCK_AFTER
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """select p.post_id, p.post_type, p.language, p.title, p.caption, p.link_url,
                          p.hashtags, p.account_id, p.queue_id, p.attempt_count,
                          a.settings, pl.key as platform_key, s.id as slot_id, s.slot_at
                   from posts p
                   join platform_accounts a on a.id = p.account_id
                   join platforms pl on pl.key = a.platform_key
                   left join queue_slots s on s.post_id = p.post_id and s.state='FILLED'
                   where p.status='PUBLISHING' and p.locked_at < :cut"""
                ),
                {"cut": now - stuck_after},
            )
            .mappings()
            .all()
        )
    for r in (dict(x) for x in rows):
        post_id = str(r["post_id"])
        settings = dict(r["settings"] or {})
        adapter = adapters(settings)
        view = PostView(
            post_id,
            str(r["post_type"]),
            str(r["language"]),
            str(r["caption"] or ""),
            r["title"],
            r["link_url"],
            tuple(r["hashtags"] or ()),
        )
        key = f"{post_id}:{r['slot_id']}"
        try:
            found = adapter.find_existing(view, PublishContext(idempotency_key=key, run_id=run_id))
        except Exception:
            found = None
        with engine.begin() as conn:
            if found is not None:
                conn.execute(
                    text(
                        """update posts set status='PUBLISHED', platform_post_id=:pp, published_url=:u,
                               published_at=:n, locked_by=null, locked_at=null where post_id=:p"""
                    ),
                    {"pp": found.platform_post_id, "u": found.url, "n": now, "p": post_id},
                )
                _audit(conn, post_id, "RECOVERED_PUBLISHED", found.platform_post_id)
                summary.published.append(post_id)
            else:
                conn.execute(
                    text(
                        """update posts set status='NEEDS_ATTENTION', locked_by=null, locked_at=null
                           where post_id=:p"""
                    ),
                    {"p": post_id},
                )
                _audit(conn, post_id, "NEEDS_ATTENTION", "stuck in PUBLISHING; not republished")
                summary.needs_attention.append(post_id)
                summary.notifications.append(("NEEDS_ATTENTION", post_id))


__all__ = ["PostStatus", "RunSummary", "kill_switch_on", "run_once"]
