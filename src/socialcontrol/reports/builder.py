"""Reports and exports (PRD-10): daily / weekly / monthly summaries from the attempt log."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from sqlalchemy import Connection, text

DHAKA = ZoneInfo("Asia/Dhaka")


@dataclass
class Report:
    kind: str  # daily | weekly | monthly | custom
    period_start: datetime  # UTC, inclusive
    period_end: datetime  # UTC, exclusive
    summary: dict[str, Any]
    by_platform: list[dict[str, Any]]
    by_queue: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    upcoming: list[dict[str, Any]]


def period_for(kind: str, ref: datetime) -> tuple[datetime, datetime]:
    """Period boundaries in Asia/Dhaka calendar terms, returned as UTC.

    ``ref`` is any instant inside the period: for daily/weekly/monthly the
    *completed* period containing it is reported.
    """
    local = ref.astimezone(DHAKA)
    if kind == "daily":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif kind == "weekly":  # Monday .. Sunday
        start = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=7)
    elif kind == "monthly":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    else:
        raise ValueError(f"unknown report kind {kind!r}")
    return start.astimezone(UTC), end.astimezone(UTC)


def previous_period(kind: str, now: datetime) -> tuple[datetime, datetime]:
    """The last completed daily/weekly/monthly period before ``now``."""
    start, _ = period_for(kind, now)
    return period_for(kind, start - timedelta(seconds=1))


def build_report(
    conn: Connection, kind: str, start: datetime, end: datetime, now: datetime | None = None
) -> Report:
    p = {"s": start, "e": end}
    counts = (
        conn.execute(
            text(
                """select
                 count(*) filter (where result in ('SUCCESS','CONFIRMED')) as published,
                 count(*) filter (where result = 'FAILED') as failed_attempts,
                 count(*) filter (where result = 'DELIVERED') as assisted_delivered,
                 count(*) filter (where result = 'CONFIRMED') as assisted_confirmed,
                 count(*) filter (where result = 'FAILED' and failure_class in ('TEMPORARY','RATE_LIMIT','UNKNOWN')) as retried
               from publish_attempts where started_at >= :s and started_at < :e"""
            ),
            p,
        )
        .mappings()
        .one()
    )
    final_failed = conn.execute(
        text(
            """select count(distinct pa.post_id) from publish_attempts pa
               join posts p on p.post_id = pa.post_id
               where pa.result = 'FAILED' and p.status = 'FAILED_FINAL'
                 and pa.started_at >= :s and pa.started_at < :e"""
        ),
        p,
    ).scalar_one()
    pending = (
        conn.execute(
            text(
                """select count(*) filter (where status in ('SCHEDULED','RETRYING')) as scheduled,
                      count(*) filter (where status='AWAITING_CONFIRMATION') as awaiting,
                      count(*) filter (where status='OVERDUE') as overdue,
                      count(*) filter (where status in ('DRAFT','IN_REVIEW')) as awaiting_review
               from posts where deleted_at is null"""
            )
        )
        .mappings()
        .one()
    )
    scheduled_in_period = conn.execute(
        text(
            "select count(*) from queue_slots where slot_at >= :s and slot_at < :e and state<>'OPEN'"
        ),
        p,
    ).scalar_one()

    by_platform = [
        dict(r)
        for r in conn.execute(
            text(
                """select platform_key, count(*) filter (where result in ('SUCCESS','CONFIRMED')) as published,
                      count(*) filter (where result='FAILED') as failed,
                      count(*) filter (where result='DELIVERED') as delivered
               from publish_attempts where started_at >= :s and started_at < :e
               group by platform_key order by platform_key"""
            ),
            p,
        ).mappings()
    ]
    by_queue = [
        dict(r)
        for r in conn.execute(
            text(
                """select q.name as queue, a.platform_key,
                      count(*) filter (where pa.result in ('SUCCESS','CONFIRMED')) as published,
                      count(*) filter (where pa.result='FAILED') as failed
               from publish_attempts pa join queues q on q.id = pa.queue_id
               join platform_accounts a on a.id = q.account_id
               where pa.started_at >= :s and pa.started_at < :e
               group by q.name, a.platform_key order by a.platform_key, q.name"""
            ),
            p,
        ).mappings()
    ]
    failures = [
        dict(r)
        for r in conn.execute(
            text(
                """select post_id, platform_key, failure_class, error_code, error_message, started_at
               from publish_attempts where result='FAILED' and started_at >= :s and started_at < :e
               order by started_at"""
            ),
            p,
        ).mappings()
    ]
    upcoming = [
        dict(r)
        for r in conn.execute(
            text(
                """select s.slot_at, s.post_id, a.platform_key, q.name as queue
               from queue_slots s join queues q on q.id = s.queue_id
               join platform_accounts a on a.id = q.account_id
               where s.state='FILLED' and s.slot_at >= :e and s.slot_at < :e + interval '7 days'
               order by s.slot_at limit 50"""
            ),
            p,
        ).mappings()
    ]

    summary = {
        "published": counts["published"],
        "failed_attempts": counts["failed_attempts"],
        "final_failures": final_failed,
        "retried": counts["retried"],
        "assisted_delivered": counts["assisted_delivered"],
        "assisted_confirmed": counts["assisted_confirmed"],
        "scheduled_in_period": scheduled_in_period,
        **dict(pending),
    }
    return Report(kind, start, end, summary, by_platform, by_queue, failures, upcoming)


def save_report(conn: Connection, report: Report) -> str:
    rid = conn.execute(
        text(
            """insert into reports (kind, period_start, period_end, summary)
               values (:k, :s, :e, cast(:j as jsonb)) returning id"""
        ),
        {
            "k": report.kind,
            "s": report.period_start.astimezone(DHAKA).date(),
            "e": (report.period_end - timedelta(seconds=1)).astimezone(DHAKA).date(),
            "j": json.dumps(report.summary, default=str),
        },
    ).scalar_one()
    return str(rid)


# ---------------------------------------------------------------- exports
def _csv_safe(value: object) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def to_json(report: Report) -> str:
    return json.dumps(
        {
            "kind": report.kind,
            "from": report.period_start.isoformat(),
            "to": report.period_end.isoformat(),
            "summary": report.summary,
            "by_platform": report.by_platform,
            "by_queue": report.by_queue,
            "failures": report.failures,
            "upcoming": report.upcoming,
        },
        default=str,
        ensure_ascii=False,
        indent=2,
    )


def to_csv(report: Report) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["section", "key", "value"])
    for k, v in report.summary.items():
        w.writerow(["summary", k, _csv_safe(v)])
    for r in report.by_platform:
        w.writerow(
            [
                "platform",
                r["platform_key"],
                f"published={r['published']};failed={r['failed']};delivered={r['delivered']}",
            ]
        )
    for r in report.by_queue:
        w.writerow(
            [
                "queue",
                f"{r['platform_key']}/{r['queue']}",
                f"published={r['published']};failed={r['failed']}",
            ]
        )
    for r in report.failures:
        w.writerow(
            [
                "failure",
                r["post_id"],
                _csv_safe(f"{r['failure_class']} {r['error_code']} {r['error_message']}"),
            ]
        )
    return out.getvalue()


def to_xlsx(report: Report) -> bytes:
    wb = Workbook(write_only=False)
    for default in list(wb.worksheets):
        wb.remove(default)
    ws = wb.create_sheet("Summary")
    ws.append(["Metric", "Value"])
    for k, v in report.summary.items():
        ws.append([k, v])
    for title, rows, cols in (
        ("By platform", report.by_platform, ["platform_key", "published", "failed", "delivered"]),
        ("By queue", report.by_queue, ["platform_key", "queue", "published", "failed"]),
        (
            "Failures",
            report.failures,
            ["post_id", "platform_key", "failure_class", "error_code", "error_message"],
        ),
        ("Upcoming", report.upcoming, ["slot_at", "post_id", "platform_key", "queue"]),
    ):
        sheet = wb.create_sheet(title)
        sheet.append(cols)
        for r in rows:
            sheet.append(
                [
                    _csv_safe(r.get(c))
                    if isinstance(r.get(c), str)
                    else (str(r.get(c)) if r.get(c) is not None else "")
                    for c in cols
                ]
            )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_text_summary(report: Report) -> str:
    """Short plain-text version for the Telegram daily summary."""
    s = report.summary
    day = report.period_start.astimezone(DHAKA).strftime("%d %b %Y")
    lines = [
        f"📊 {report.kind.title()} report — {day}",
        f"Published: {s['published']}   Failed: {s['final_failures']}   Retrying: {s['retried']}",
        f"Assisted: {s['assisted_delivered']} delivered / {s['assisted_confirmed']} confirmed",
        f"Pending: {s['scheduled']} scheduled · {s['awaiting']} awaiting you · {s['overdue']} overdue",
        f"Review queue: {s['awaiting_review']}",
    ]
    for r in report.by_platform:
        lines.append(f"  • {r['platform_key']}: {r['published']} ok / {r['failed']} failed")
    return "\n".join(lines)
