from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from socialcontrol.scheduler.recurrence import RecurrenceError, next_occurrences, parse_rule

DHAKA = ZoneInfo("Asia/Dhaka")
LONDON = ZoneInfo("Europe/London")
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def local_dates(rule_dict, tz, start, n=5, after=EPOCH):
    rule = parse_rule(rule_dict)
    out = next_occurrences(rule, tz, start, after, n)
    return [o.astimezone(tz).strftime("%Y-%m-%d %H:%M") for o in out]


def test_vector1_every_7_days():
    got = local_dates(
        {"type": "interval_days", "every": 7, "time_local": "10:00"}, DHAKA, datetime(2026, 10, 1)
    )
    assert got == [
        "2026-10-01 10:00",
        "2026-10-08 10:00",
        "2026-10-15 10:00",
        "2026-10-22 10:00",
        "2026-10-29 10:00",
    ]


def test_vector2_weekly_mon_thu():
    got = local_dates(
        {"type": "weekly", "weekdays": ["MON", "THU"], "time_local": "11:00"},
        DHAKA,
        datetime(2026, 10, 5),
    )
    assert got == [
        "2026-10-05 11:00",
        "2026-10-08 11:00",
        "2026-10-12 11:00",
        "2026-10-15 11:00",
        "2026-10-19 11:00",
    ]


def test_vector3_every_3_days_evening():
    got = local_dates(
        {"type": "interval_days", "every": 3, "time_local": "20:00"}, DHAKA, datetime(2026, 10, 1)
    )
    assert got[:3] == ["2026-10-01 20:00", "2026-10-04 20:00", "2026-10-07 20:00"]


def test_vector4_monthly_day_31_clamped():
    got = local_dates(
        {"type": "monthly_day", "day": 31, "clamp": "last_day", "time_local": "09:00"},
        DHAKA,
        datetime(2026, 1, 31),
    )
    assert got == [
        "2026-01-31 09:00",
        "2026-02-28 09:00",
        "2026-03-31 09:00",
        "2026-04-30 09:00",
        "2026-05-31 09:00",
    ]


def test_monthly_day_31_skip_mode():
    got = local_dates(
        {"type": "monthly_day", "day": 31, "clamp": "skip", "time_local": "09:00"},
        DHAKA,
        datetime(2026, 1, 31),
    )
    assert got[:3] == ["2026-01-31 09:00", "2026-03-31 09:00", "2026-05-31 09:00"]


def test_vector5_second_tuesday():
    got = local_dates(
        {"type": "monthly_nth", "nth": 2, "weekday": "TUE", "time_local": "10:00"},
        DHAKA,
        datetime(2026, 10, 1),
    )
    assert got == [
        "2026-10-13 10:00",
        "2026-11-10 10:00",
        "2026-12-08 10:00",
        "2027-01-12 10:00",
        "2027-02-09 10:00",
    ]


def test_last_friday():
    got = local_dates(
        {"type": "monthly_nth", "nth": -1, "weekday": "FRI", "time_local": "10:00"},
        DHAKA,
        datetime(2026, 10, 1),
        n=2,
    )
    assert got == ["2026-10-30 10:00", "2026-11-27 10:00"]


def test_vector6_daily_skip_friday():
    got = local_dates(
        {"type": "interval_days", "every": 1, "skip_weekdays": ["FRI"], "time_local": "10:00"},
        DHAKA,
        datetime(2026, 10, 1),
    )
    # 2026-10-02 is a Friday
    assert got == [
        "2026-10-01 10:00",
        "2026-10-03 10:00",
        "2026-10-04 10:00",
        "2026-10-05 10:00",
        "2026-10-06 10:00",
    ]


def test_skip_dates_and_blackout():
    got = local_dates(
        {
            "type": "interval_days",
            "every": 1,
            "time_local": "10:00",
            "skip_dates": ["2026-10-02"],
            "blackout_ranges": [["2026-10-04", "2026-10-06"]],
        },
        DHAKA,
        datetime(2026, 10, 1),
        n=3,
    )
    assert got == ["2026-10-01 10:00", "2026-10-03 10:00", "2026-10-07 10:00"]


def test_vector7_dst_gap_london_shifted_forward():
    # 2027-03-28 01:30 does not exist in London (clocks 01:00 -> 02:00).
    got = local_dates(
        {"type": "interval_days", "every": 1, "time_local": "01:30"},
        LONDON,
        datetime(2027, 3, 27),
        n=3,
    )
    assert got == ["2027-03-27 01:30", "2027-03-28 02:30", "2027-03-29 01:30"]


def test_dst_fold_uses_first_occurrence():
    # 2026-10-25 01:30 happens twice in London; first occurrence is BST (UTC+1).
    rule = parse_rule({"type": "once", "time_local": "01:30"})
    out = next_occurrences(rule, LONDON, datetime(2026, 10, 25), EPOCH, 1)
    assert out[0] == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_after_is_strictly_exclusive():
    rule = parse_rule({"type": "interval_days", "every": 7, "time_local": "10:00"})
    first = next_occurrences(rule, DHAKA, datetime(2026, 10, 1), EPOCH, 1)[0]
    nxt = next_occurrences(rule, DHAKA, datetime(2026, 10, 1), first, 1)[0]
    assert nxt - first == timedelta(days=7)


def test_anchor_stable_regardless_of_after():
    rule = parse_rule({"type": "interval_days", "every": 7, "time_local": "10:00"})
    start = datetime(2026, 10, 1)
    full = next_occurrences(rule, DHAKA, start, EPOCH, 10)
    later = next_occurrences(rule, DHAKA, start, full[3], 6)
    assert later == full[4:10]


def test_until_bound_and_limit():
    rule = parse_rule({"type": "interval_days", "every": 1, "time_local": "10:00"})
    until = datetime(2026, 10, 3, 23, 59, tzinfo=DHAKA)
    out = next_occurrences(rule, DHAKA, datetime(2026, 10, 1), EPOCH, 100, until_utc=until)
    assert len(out) == 3
    assert next_occurrences(rule, DHAKA, datetime(2026, 10, 1), EPOCH, 0) == []


def test_time_defaults_from_start():
    rule = parse_rule({"type": "interval_days", "every": 1})
    out = next_occurrences(rule, DHAKA, datetime(2026, 10, 1, 8, 45), EPOCH, 1)
    assert out[0].astimezone(DHAKA).strftime("%H:%M") == "08:45"


def test_everything_skipped_terminates():
    rule = parse_rule(
        {
            "type": "interval_days",
            "every": 1,
            "time_local": "10:00",
            "skip_weekdays": list(["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]),
        }
    )
    assert next_occurrences(rule, DHAKA, datetime(2026, 10, 1), EPOCH, 5) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "nope"},
        {"type": "interval_days", "every": 0},
        {"type": "weekly", "weekdays": []},
        {"type": "weekly", "weekdays": ["FUNDAY"]},
        {"type": "monthly_day", "day": 32},
        {"type": "monthly_nth", "nth": 9, "weekday": "MON"},
        {"type": "once", "time_local": "25:99"},
        {"type": "once", "blackout_ranges": [["2026-10-05", "2026-10-01"]]},
    ],
)
def test_invalid_rules_rejected(bad):
    with pytest.raises(RecurrenceError):
        parse_rule(bad)


def test_naive_after_rejected():
    rule = parse_rule({"type": "once"})
    with pytest.raises(ValueError):
        next_occurrences(rule, DHAKA, datetime(2026, 10, 1), datetime(2026, 1, 1), 1)


rules = st.one_of(
    st.builds(
        lambda e: {"type": "interval_days", "every": e, "time_local": "10:00"}, st.integers(1, 40)
    ),
    st.builds(
        lambda w, e: {"type": "weekly", "every_weeks": e, "weekdays": w, "time_local": "11:30"},
        st.lists(
            st.sampled_from(["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]),
            min_size=1,
            max_size=4,
        ),
        st.integers(1, 4),
    ),
    st.builds(
        lambda d: {"type": "monthly_day", "day": d, "clamp": "last_day", "time_local": "09:00"},
        st.integers(1, 31),
    ),
)


@given(
    rules,
    st.sampled_from([DHAKA, LONDON, ZoneInfo("America/New_York")]),
    st.dates(min_value=datetime(2025, 1, 1).date(), max_value=datetime(2030, 1, 1).date()),
)
def test_property_monotonic_unique_stable(rule_dict, tz, start_date):
    rule = parse_rule(rule_dict)
    start = datetime.combine(start_date, datetime.min.time())
    out = next_occurrences(rule, tz, start, EPOCH, 30)
    assert len(out) == 30
    assert out == sorted(out)
    assert len(set(out)) == len(out)
    # re-deriving from the middle gives the same tail (anchor stability)
    tail = next_occurrences(rule, tz, start, out[9], 20)
    assert tail == out[10:30]
