"""Recurrence engine (TDD-03 section 2).

Pure functions: no I/O, no database. Dates are generated in the queue's local
timezone from a fixed anchor (the queue start date) so slots never drift, then
converted to UTC.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
RULE_TYPES = {"once", "interval_days", "weekly", "monthly_day", "monthly_nth"}
MAX_SCAN = 50_000  # safety cap on candidate dates scanned (e.g. rules that skip everything)


class RecurrenceError(ValueError):
    """Invalid recurrence rule."""


@dataclass(frozen=True)
class Rule:
    type: str
    every: int = 1
    weekdays: tuple[int, ...] = ()
    day: int = 1
    nth: int = 1
    weekday: int = 0
    clamp: str = "last_day"  # "last_day" or "skip"
    time_local: time | None = None
    skip_dates: frozenset[date] = field(default_factory=frozenset)
    skip_weekdays: frozenset[int] = field(default_factory=frozenset)
    blackout: tuple[tuple[date, date], ...] = ()


def _weekday(name: str) -> int:
    try:
        return WEEKDAYS[name.upper()]
    except KeyError as exc:
        raise RecurrenceError(f"unknown weekday: {name!r}") from exc


def _positive(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RecurrenceError(f"{label} must be a positive integer")
    return value


def parse_rule(data: Mapping[str, Any]) -> Rule:
    """Build a validated Rule from the JSON stored in queues.recurrence."""
    rtype = data.get("type")
    if rtype not in RULE_TYPES:
        raise RecurrenceError(f"unknown recurrence type: {rtype!r}")

    t_local: time | None = None
    if data.get("time_local"):
        try:
            hh, mm = str(data["time_local"]).split(":")
            t_local = time(int(hh), int(mm))
        except ValueError as exc:
            raise RecurrenceError("time_local must be 'HH:MM'") from exc

    kwargs: dict[str, Any] = {"type": rtype, "time_local": t_local}

    if rtype == "interval_days":
        kwargs["every"] = _positive(data.get("every"), "every")
    elif rtype == "weekly":
        kwargs["every"] = _positive(data.get("every_weeks", 1), "every_weeks")
        days = data.get("weekdays")
        if not days:
            raise RecurrenceError("weekly rule needs weekdays")
        kwargs["weekdays"] = tuple(sorted({_weekday(d) for d in days}))
    elif rtype == "monthly_day":
        kwargs["every"] = _positive(data.get("every_months", 1), "every_months")
        day = _positive(data.get("day"), "day")
        if day > 31:
            raise RecurrenceError("day must be 1..31")
        kwargs["day"] = day
        clamp = data.get("clamp", "last_day")
        if clamp not in ("last_day", "skip"):
            raise RecurrenceError("clamp must be 'last_day' or 'skip'")
        kwargs["clamp"] = clamp
    elif rtype == "monthly_nth":
        kwargs["every"] = _positive(data.get("every_months", 1), "every_months")
        nth = data.get("nth")
        if nth not in (1, 2, 3, 4, 5, -1):
            raise RecurrenceError("nth must be 1..5 or -1")
        kwargs["nth"] = nth
        kwargs["weekday"] = _weekday(str(data.get("weekday", "")))

    try:
        kwargs["skip_dates"] = frozenset(date.fromisoformat(d) for d in data.get("skip_dates", []))
        kwargs["skip_weekdays"] = frozenset(_weekday(d) for d in data.get("skip_weekdays", []))
        kwargs["blackout"] = tuple(
            (date.fromisoformat(a), date.fromisoformat(b))
            for a, b in data.get("blackout_ranges", [])
        )
    except ValueError as exc:
        raise RecurrenceError(f"invalid skip/blackout date: {exc}") from exc
    for a, b in kwargs["blackout"]:
        if b < a:
            raise RecurrenceError("blackout range end before start")
    return Rule(**kwargs)


# ---------------------------------------------------------------- candidate dates


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date | None:
    last_day = calendar.monthrange(year, month)[1]
    days = [
        date(year, month, d)
        for d in range(1, last_day + 1)
        if date(year, month, d).weekday() == weekday
    ]
    if nth == -1:
        return days[-1]
    return days[nth - 1] if nth <= len(days) else None


def _candidates(rule: Rule, anchor: date) -> Iterator[date]:
    """Ascending candidate local dates (>= anchor), before skip filtering."""
    if rule.type == "once":
        yield anchor
        return

    if rule.type == "interval_days":
        k = 0
        while True:
            yield anchor + timedelta(days=rule.every * k)
            k += 1

    elif rule.type == "weekly":
        week_start = anchor - timedelta(days=anchor.weekday())
        k = 0
        while True:
            base = week_start + timedelta(weeks=rule.every * k)
            for wd in rule.weekdays:
                d = base + timedelta(days=wd)
                if d >= anchor:
                    yield d
            k += 1

    elif rule.type == "monthly_day":
        k = 0
        while True:
            y, m = _add_months(anchor.year, anchor.month, rule.every * k)
            last = calendar.monthrange(y, m)[1]
            if rule.day <= last:
                d = date(y, m, rule.day)
            elif rule.clamp == "last_day":
                d = date(y, m, last)
            else:
                d = None
            if d is not None and d >= anchor:
                yield d
            k += 1

    elif rule.type == "monthly_nth":
        k = 0
        while True:
            y, m = _add_months(anchor.year, anchor.month, rule.every * k)
            d = _nth_weekday(y, m, rule.weekday, rule.nth)
            if d is not None and d >= anchor:
                yield d
            k += 1


def _is_skipped(rule: Rule, d: date) -> bool:
    if d in rule.skip_dates or d.weekday() in rule.skip_weekdays:
        return True
    return any(a <= d <= b for a, b in rule.blackout)


# ---------------------------------------------------------------- local -> UTC


def local_to_utc(d: date, t: time, tz: ZoneInfo) -> datetime:
    """Resolve a local wall time to UTC.

    Nonexistent time (DST gap): shifted forward by the gap length (Python fold=0
    semantics). Ambiguous time (DST fold): the first occurrence.
    """
    aware = datetime.combine(d, t, tzinfo=tz)  # fold=0
    return aware.astimezone(UTC)


# ---------------------------------------------------------------- public API


def next_occurrences(
    rule: Rule,
    tz: ZoneInfo,
    start_local: datetime,
    after_utc: datetime,
    limit: int,
    until_utc: datetime | None = None,
) -> list[datetime]:
    """Next ``limit`` occurrences strictly after ``after_utc`` (ascending, UTC).

    ``start_local`` is a naive local datetime: its date is the anchor, its time
    is used when the rule has no ``time_local``.
    """
    if limit < 1:
        return []
    if after_utc.tzinfo is None or (until_utc is not None and until_utc.tzinfo is None):
        raise ValueError("after_utc/until_utc must be timezone-aware")
    if start_local.tzinfo is not None:
        raise ValueError("start_local must be naive local time")

    at = rule.time_local or start_local.time().replace(second=0, microsecond=0)
    anchor = start_local.date()
    found: list[datetime] = []
    scanned = 0
    for d in _candidates(rule, anchor):
        scanned += 1
        if scanned > MAX_SCAN:
            break
        if _is_skipped(rule, d):
            continue
        utc = local_to_utc(d, at, tz)
        if until_utc is not None and utc > until_utc:
            break
        if utc <= after_utc:
            continue
        found.append(utc)
        if len(found) >= limit:
            break
    return found
