"""Scheduled jobs: publisher cycle, reports, watchdog (PRD-07/09/10, OPS-01).

These are what GitHub Actions (or a local scheduler) run. They orchestrate the engine and the
notification router; all decisions live in the modules they call.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from socialcontrol.assisted import service as assisted
from socialcontrol.notifications.router import Router, format_event
from socialcontrol.platforms.base import MediaView, PlatformAdapter, PostView
from socialcontrol.publisher.engine import RunSummary, run_once
from socialcontrol.reports import builder
from socialcontrol.scheduler import queue_service

AdapterFactory = Callable[[dict[str, Any]], PlatformAdapter]


def _post_view(engine: Engine, post_id: str) -> tuple[PostView, dict[str, Any]]:
    with engine.connect() as c:
        row = dict(
            c.execute(
                text(
                    """select p.*, a.platform_key, a.destination_url, a.mode, a.settings
                       from posts p join platform_accounts a on a.id = p.account_id
                       where p.post_id = :p"""
                ),
                {"p": post_id},
            )
            .mappings()
            .one()
        )
        media = tuple(
            MediaView(url=r[0] or r[1], mime=r[2], sha256=r[3])
            for r in c.execute(
                text(
                    """select m.public_url, m.storage_key, m.mime, m.sha256 from post_media pm
                       join media m on m.id = pm.media_id where pm.post_id = :p order by pm.sort"""
                ),
                {"p": post_id},
            )
        )
    view = PostView(
        post_id=post_id,
        post_type=row["post_type"],
        language=row["language"],
        caption=row["caption"] or "",
        title=row["title"],
        link_url=row["link_url"],
        hashtags=tuple(row["hashtags"] or ()),
        media=media,
    )
    return view, row


def _assisted_message(
    adapter: PlatformAdapter, view: PostView, row: dict[str, Any], link: str, header: str
) -> tuple[str, str]:
    pkg = adapter.build_assisted_package(view)
    lines = [pkg.text]
    if pkg.media_links:
        lines.append("\nMedia: " + "\n".join(pkg.media_links))
    if pkg.destination_url:
        lines.append(f"\nOpen: {pkg.destination_url}")
    if pkg.char_limit:
        lines.append(f"({pkg.char_count}/{pkg.char_limit} characters)")
    lines.extend(f"• {h}" for h in pkg.hints)
    lines.append(f"\nWhen done, tap: {link}")
    return f"{header} {row['platform_key']} · {view.post_id}", "\n".join(lines)


def run_publisher_cycle(
    engine: Engine,
    adapters: AdapterFactory,
    router: Router,
    now: datetime,
    signing_key: str,
    base_url: str,
    **engine_kwargs: Any,
) -> RunSummary:
    """One full cycle: publish/deliver, send notifications and assisted packages, reminders."""
    summary = run_once(engine, adapters, now, signing_key=signing_key, **engine_kwargs)

    sent_assisted: set[str] = set()
    for post_id in summary.delivered:
        view, row = _post_view(engine, post_id)
        token = summary.assisted_tokens.get(post_id)
        if not token:
            continue
        adapter = adapters(
            {
                "_platform_key": row["platform_key"],
                "_mode": "ASSISTED",
                "destination_url": row["destination_url"],
            }
        )
        subject, body = _assisted_message(
            adapter, view, row, f"{base_url}/a/{token}", "📝 Post now:"
        )
        with engine.begin() as c:
            router.dispatch(c, "ASSISTED_DUE", post_id, subject, body, now)
        sent_assisted.add(post_id)

    for event, ref in summary.notifications:
        if event == "ASSISTED_DUE":
            continue  # already sent with the full package
        detail, link = "", f"{base_url}/posts/{ref}"
        if event == "FINAL_FAILURE":
            with engine.connect() as c:
                detail = str(
                    c.execute(
                        text("""select coalesce(failure_class,'') || ' ' || coalesce(error_message,'')
                            from publish_attempts where post_id=:p and result='FAILED'
                            order by started_at desc limit 1"""),
                        {"p": ref},
                    ).scalar()
                    or ""
                )
        elif event == "RUNWAY_LOW":
            with engine.connect() as c:
                name = c.execute(
                    text("select name from queues where id=cast(:i as uuid)"), {"i": ref}
                ).scalar()
                days = queue_service.runway_days(c, ref, now)
            detail, link = (
                f"Queue '{name}' has about {days:.0f} day(s) of content left.",
                f"{base_url}/queues",
            )
        elif event == "EMPTY_SLOT":
            with engine.connect() as c:
                slot_row = c.execute(
                    text(
                        """select s.slot_at, q.name from queue_slots s join queues q on q.id=s.queue_id
                       where s.id = cast(:i as uuid)"""
                    ),
                    {"i": ref},
                ).first()
            detail = (
                f"No approved post was available for {slot_row[1]} at {slot_row[0]:%d %b %H:%M} UTC."
                if slot_row
                else ""
            )
            link = f"{base_url}/queues"
        subject, body = format_event(event, ref, detail, link)
        with engine.begin() as c:
            router.dispatch(c, event, ref, subject, body, now)

    with engine.begin() as c:
        reminders = assisted.process_reminders(c, now)
    for r in reminders:
        view, row = _post_view(engine, r.post_id)
        with engine.connect() as c:
            task_id = c.execute(
                text(
                    "select id::text from assisted_tasks where post_id=:p order by delivered_at desc limit 1"
                ),
                {"p": r.post_id},
            ).scalar()
            token = assisted.rebuild_token(c, str(task_id), signing_key) if task_id else None
        if not token:
            continue
        adapter = adapters(
            {
                "_platform_key": row["platform_key"],
                "_mode": "ASSISTED",
                "destination_url": row["destination_url"],
            }
        )
        header = "⚠️ Overdue:" if r.overdue else f"⏰ Reminder {r.number}:"
        subject, body = _assisted_message(adapter, view, row, f"{base_url}/a/{token}", header)
        with engine.begin() as c:
            router.dispatch(c, "ASSISTED_REMINDER", f"{r.post_id}:{r.number}", subject, body, now)
    return summary


def run_report(engine: Engine, router: Router, kind: str, now: datetime) -> builder.Report:
    """Build and store the report for the last completed period and send the summary."""
    start, end = builder.previous_period(kind, now)
    with engine.begin() as c:
        report = builder.build_report(c, kind, start, end)
        builder.save_report(c, report)
    subject = f"SocialControl {kind} report"
    body = builder.to_text_summary(report)
    event = {"daily": "DAILY_REPORT", "weekly": "WEEKLY_REPORT", "monthly": "MONTHLY_REPORT"}[kind]
    with engine.begin() as c:
        router.dispatch(c, event, start.date().isoformat(), subject, body, now)
    return report


def watchdog(
    engine: Engine, router: Router, now: datetime, max_age: timedelta = timedelta(minutes=70)
) -> list[str]:
    """Independent checks that must alert even if the publisher itself is dead (PUB-11)."""
    problems: list[str] = []
    with engine.connect() as c:
        last = c.execute(
            text("select max(started_at) from job_runs where job='publisher'")
        ).scalar()
        expired = c.execute(
            text("select count(*) from platform_accounts where state in ('TOKEN_EXPIRED','ERROR')")
        ).scalar_one()
        low = queue_service.queues_low_on_runway(c, now)
        db_mb = c.execute(
            text("select pg_database_size(current_database()) / 1048576.0")
        ).scalar_one()
    if last is None or now - last > max_age:
        problems.append("HEARTBEAT_MISSED")
        subject, body = format_event("HEARTBEAT_MISSED", detail=f"Last publisher run: {last}")
        with engine.begin() as c:
            router.dispatch(c, "HEARTBEAT_MISSED", "publisher", subject, body, now)
    if expired:
        problems.append("TOKEN_EXPIRED")
        subject, body = format_event(
            "TOKEN_EXPIRED", detail=f"{expired} account(s) need reconnecting"
        )
        with engine.begin() as c:
            router.dispatch(c, "TOKEN_EXPIRED", "accounts", subject, body, now)
    for qid, days, threshold in low:
        problems.append(f"RUNWAY_LOW:{qid}")
        subject, body = format_event(
            "RUNWAY_LOW", detail=f"{days:.0f} days left (alert below {threshold})"
        )
        with engine.begin() as c:
            router.dispatch(c, "RUNWAY_LOW", qid, subject, body, now)
    if db_mb > 400:  # free tier is ~500 MB
        problems.append("STORAGE_HIGH")
        with engine.begin() as c:
            router.dispatch(
                c, "STORAGE_HIGH", "db", "Database size is high", f"{db_mb:.0f} MB used", now
            )
    return problems
