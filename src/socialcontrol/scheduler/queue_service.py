"""Queue service: ties the pure scheduler logic to the database (PRD-05, TDD-03).

All functions take an open SQLAlchemy ``Connection`` (the caller owns the
transaction) and an injectable ``now``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from socialcontrol.domain.enums import PatternMode, SlotState
from socialcontrol.scheduler.recurrence import next_occurrences, parse_rule
from socialcontrol.scheduler.slots import Candidate, Slot, allocate, compact

MIN_LEAD = timedelta(minutes=30)


@dataclass
class AllocationSummary:
    scheduled: list[str]
    unassigned: list[str]
    slots_created: int = 0


# ---------------------------------------------------------------- helpers
def _lock(conn: Connection, queue_id: Any) -> None:
    conn.execute(text("select pg_advisory_xact_lock(hashtext(:q))"), {"q": str(queue_id)})


def _queue(conn: Connection, queue_id: Any) -> dict[str, Any]:
    row = conn.execute(text("select * from queues where id = :q"), {"q": queue_id}).mappings().one()
    return dict(row)


def _pattern(q: dict[str, Any]) -> list[str]:
    value = q["pattern"]
    return [str(x) for x in value] if isinstance(value, list) else []


# ---------------------------------------------------------------- slot materialisation
def ensure_horizon(conn: Connection, queue_id: Any, now: datetime) -> int:
    """Create OPEN slots up to ``now + horizon_days`` (idempotent). Returns rows created."""
    q = _queue(conn, queue_id)
    if q["status"] not in ("ACTIVE", "PAUSED"):
        return 0
    tz = ZoneInfo(q["timezone"])
    rule = parse_rule(q["recurrence"])
    start_local = q["start_at"].astimezone(tz).replace(tzinfo=None)

    last = conn.execute(
        text("select max(slot_at) from queue_slots where queue_id = :q"), {"q": queue_id}
    ).scalar_one()
    after = last if last is not None else q["start_at"] - timedelta(seconds=1)
    until = now + timedelta(days=q["horizon_days"])
    if q["end_at"] is not None:
        until = min(until, q["end_at"])

    limit = 10_000
    if q["max_posts"] is not None:
        used = conn.execute(
            text("select count(*) from queue_slots where queue_id = :q"), {"q": queue_id}
        ).scalar_one()
        limit = max(0, q["max_posts"] - used)
    times = next_occurrences(rule, tz, start_local, after, limit, until_utc=until) if limit else []
    for t in times:
        conn.execute(
            text(
                """insert into queue_slots (queue_id, slot_at) values (:q, :t)
                   on conflict (queue_id, slot_at) do nothing"""
            ),
            {"q": queue_id, "t": t},
        )
    return len(times)


# ---------------------------------------------------------------- state load / persist
def _load_slots(conn: Connection, queue_id: Any, since: datetime) -> list[tuple[Any, Slot]]:
    rows = conn.execute(
        text(
            """select id, slot_at, state, post_id, pattern_index, filled_by
               from queue_slots where queue_id = :q and slot_at >= :s order by slot_at"""
        ),
        {"q": queue_id, "s": since},
    ).mappings()
    return [
        (
            r["id"],
            Slot(
                r["slot_at"],
                SlotState(r["state"]),
                r["post_id"],
                r["pattern_index"],
                r["filled_by"],
            ),
        )
        for r in rows
    ]


def _persist(
    conn: Connection,
    queue_id: Any,
    pairs: list[tuple[Any, Slot]],
    before: dict[Any, tuple[str, str | None]],
    scheduled: set[str],
    released: set[str],
) -> None:
    """Write slot/post changes. Slots are emptied first so the one-slot-per-post index never trips."""
    changed = [(sid, s) for sid, s in pairs if (s.state.value, s.post_id) != before[sid]]
    for sid, _s in changed:
        conn.execute(
            text("update queue_slots set state='OPEN', post_id=null, filled_by=null where id=:i"),
            {"i": sid},
        )
    for sid, s in changed:
        if s.state == SlotState.FILLED:
            conn.execute(
                text(
                    """update queue_slots set state='FILLED', post_id=:p, pattern_index=:pi,
                           filled_by=:fb where id=:i"""
                ),
                {"i": sid, "p": s.post_id, "pi": s.pattern_index, "fb": s.filled_by},
            )
            conn.execute(
                text(
                    """update posts set status='SCHEDULED', scheduled_at=:t where post_id=:p
                       and status in ('APPROVED','QUEUED','SCHEDULED')"""
                ),
                {"p": s.post_id, "t": s.slot_at},
            )
    for pid in released:
        conn.execute(
            text(
                """update posts set status='APPROVED', scheduled_at=null where post_id=:p
                   and status='SCHEDULED'"""
            ),
            {"p": pid},
        )
    _ = scheduled, queue_id


# ---------------------------------------------------------------- allocation
def allocate_queue(conn: Connection, queue_id: Any, now: datetime) -> AllocationSummary:
    """Give APPROVED unslotted posts of the queue the next free slots (QUE-05..07)."""
    _lock(conn, queue_id)
    q = _queue(conn, queue_id)
    created = ensure_horizon(conn, queue_id, now)
    if q["status"] != "ACTIVE":
        return AllocationSummary([], [], created)

    pairs = _load_slots(conn, queue_id, now)
    before = {sid: (s.state.value, s.post_id) for sid, s in pairs}
    cand_rows = conn.execute(
        text(
            """select p.post_id, p.post_type, coalesce(p.queue_position, 0) as pos,
                      p.pinned, p.scheduled_at
               from posts p
               where p.queue_id = :q and p.status in ('APPROVED','QUEUED') and p.deleted_at is null
                 and not exists (select 1 from queue_slots s where s.post_id = p.post_id
                                 and s.state = 'FILLED')
               order by p.queue_position nulls last, p.post_id"""
        ),
        {"q": queue_id},
    ).mappings()
    candidates = [
        Candidate(
            r["post_id"], r["post_type"], r["pos"], r["scheduled_at"] if r["pinned"] else None
        )
        for r in cand_rows
    ]
    result = allocate(
        [s for _, s in pairs],
        candidates,
        _pattern(q),
        q["pattern_pointer"],
        PatternMode(q["pattern_mode"]),
        now,
        MIN_LEAD,
    )
    scheduled = {pid for _, pid in result.assignments}
    _persist(conn, queue_id, pairs, before, scheduled, set())
    conn.execute(
        text("update queues set pattern_pointer = :p where id = :q"),
        {"p": result.pointer, "q": queue_id},
    )
    return AllocationSummary(sorted(scheduled), result.unassigned, created)


def release_post(
    conn: Connection, queue_id: Any, post_id: str, now: datetime, new_status: str
) -> None:
    """Take a post out of the schedule (cancel / skip / edit) and compact the queue.

    ``new_status`` is the post's new status (CANCELLED, SKIPPED, IN_REVIEW ...).
    With ``keep_holes`` the freed slot stays open; otherwise later posts shift up.
    """
    _lock(conn, queue_id)
    q = _queue(conn, queue_id)
    conn.execute(
        text(
            """update queue_slots set state='OPEN', post_id=null, filled_by=null
               where post_id=:p and state='FILLED'"""
        ),
        {"p": post_id},
    )
    conn.execute(
        text("update posts set status=:s, scheduled_at=null where post_id=:p"),
        {"s": new_status, "p": post_id},
    )
    if q["keep_holes"] or q["status"] != "ACTIVE":
        return

    pairs = _load_slots(conn, queue_id, now)
    before = {sid: (s.state.value, s.post_id) for sid, s in pairs}
    order_rows = conn.execute(
        text(
            """select p.post_id, p.post_type, coalesce(p.queue_position, 0) as pos
               from posts p join queue_slots s on s.post_id = p.post_id and s.state='FILLED'
               where s.queue_id = :q and p.status = 'SCHEDULED' and s.slot_at >= :n"""
        ),
        {"q": queue_id, "n": now},
    ).mappings()
    order = {r["post_id"]: Candidate(r["post_id"], r["post_type"], r["pos"]) for r in order_rows}
    # keep the original FIFO order: slot time order of the currently filled slots
    slot_order = {s.post_id: i for i, (_, s) in enumerate(pairs) if s.post_id}
    ordered = {
        pid: Candidate(c.post_id, c.post_type, slot_order.get(pid, 0)) for pid, c in order.items()
    }
    result = compact(
        [s for _, s in pairs], ordered, _pattern(q), PatternMode(q["pattern_mode"]), now, MIN_LEAD
    )
    _persist(conn, queue_id, pairs, before, set(), set(result.unassigned))
    conn.execute(
        text("update queues set pattern_pointer = :p where id = :q"),
        {"p": result.pointer, "q": queue_id},
    )


# ---------------------------------------------------------------- empty slots / evergreen
def fill_empty_slots(conn: Connection, queue_id: Any, now: datetime) -> dict[str, list[str]]:
    """Slots that are due but empty: evergreen reuse if enabled, else mark EMPTY (QUE-10).

    Returns {"evergreen": [new post ids], "empty": [slot ids]}.
    """
    _lock(conn, queue_id)
    q = _queue(conn, queue_id)
    out: dict[str, list[str]] = {"evergreen": [], "empty": []}
    if q["status"] != "ACTIVE":
        return out
    grace_floor = now - timedelta(hours=6)
    due = (
        conn.execute(
            text(
                """select id, slot_at from queue_slots where queue_id=:q and state='OPEN'
               and slot_at <= :n and slot_at > :g order by slot_at"""
            ),
            {"q": queue_id, "n": now, "g": grace_floor},
        )
        .mappings()
        .all()
    )
    for slot in due:
        pick = None
        if q["evergreen_enabled"]:
            pick = _pick_evergreen(conn, q, now)
        if pick is None:
            conn.execute(
                text("update queue_slots set state='EMPTY' where id=:i"), {"i": slot["id"]}
            )
            out["empty"].append(str(slot["id"]))
            continue
        new_id = _clone_for_reuse(conn, pick, q["id"])
        conn.execute(
            text(
                """update queue_slots set state='FILLED', post_id=:p, filled_by='EVERGREEN'
                   where id=:i"""
            ),
            {"p": new_id, "i": slot["id"]},
        )
        conn.execute(
            text("update posts set scheduled_at=:t where post_id=:p"),
            {"t": slot["slot_at"], "p": new_id},
        )
        out["evergreen"].append(new_id)
    return out


def _pick_evergreen(conn: Connection, q: dict[str, Any], now: datetime) -> str | None:
    """Oldest-published eligible evergreen original; never the queue's previous publication."""
    cutoff = now - timedelta(days=q["evergreen_gap_days"])
    row = conn.execute(
        text(
            """with pubs as (
                 select coalesce(source_post_id, post_id) as origin, max(published_at) as last_pub
                 from posts where status='PUBLISHED' and account_id = :a and published_at is not null
                 group by 1),
               prev as (
                 select coalesce(source_post_id, post_id) as origin from posts
                 where status in ('PUBLISHED','SCHEDULED','AWAITING_CONFIRMATION') and queue_id = :q
                 order by coalesce(published_at, scheduled_at) desc limit 1)
               select o.post_id from posts o join pubs on pubs.origin = o.post_id
               where o.evergreen and o.account_id = :a and o.source_post_id is null
                 and pubs.last_pub <= :cut
                 and o.post_id not in (select origin from prev)
               order by pubs.last_pub asc, o.post_id limit 1"""
        ),
        {"a": q["account_id"], "q": q["id"], "cut": cutoff},
    ).first()
    return str(row[0]) if row else None


def _clone_for_reuse(conn: Connection, origin_id: str, queue_id: Any) -> str:
    n = conn.execute(
        text("select count(*) from posts where source_post_id = :o"), {"o": origin_id}
    ).scalar_one()
    new_id = f"{origin_id}-R{n + 1}"
    conn.execute(
        text(
            """insert into posts (post_id, content_id, account_id, queue_id, post_type, language,
                   title, caption, link_url, hashtags, evergreen, status, approved_at, approved_hash,
                   source_post_id)
               select :n, content_id, account_id, :q, post_type, language, title, caption, link_url,
                      hashtags, false, 'SCHEDULED', now(), approved_hash, post_id
               from posts where post_id = :o"""
        ),
        {"n": new_id, "o": origin_id, "q": queue_id},
    )
    conn.execute(
        text(
            """insert into post_media (post_id, media_id, role, sort)
               select :n, media_id, role, sort from post_media where post_id = :o"""
        ),
        {"n": new_id, "o": origin_id},
    )
    return new_id


# ---------------------------------------------------------------- runway
def runway_days(conn: Connection, queue_id: Any, now: datetime) -> float:
    """Days from now to the last FILLED slot (0 if nothing scheduled) (QUE-11)."""
    last = conn.execute(
        text(
            "select max(slot_at) from queue_slots where queue_id=:q and state='FILLED' and slot_at > :n"
        ),
        {"q": queue_id, "n": now},
    ).scalar_one()
    if last is None:
        return 0.0
    return float(max(0.0, (last - now).total_seconds() / 86400))


def queues_low_on_runway(conn: Connection, now: datetime) -> list[tuple[str, float, int]]:
    """(queue_id, runway_days, threshold) for ACTIVE queues below their threshold."""
    rows = conn.execute(
        text("select id, runway_threshold_days from queues where status='ACTIVE'")
    ).all()
    low = []
    for qid, threshold in rows:
        d = runway_days(conn, qid, now)
        if d < threshold:
            low.append((str(qid), d, threshold))
    return low


def utcnow() -> datetime:
    return datetime.now(UTC)
