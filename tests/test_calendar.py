"""Calendar correctness. Half-days and DST are position-losing bugs, not cosmetics."""

from __future__ import annotations

import datetime as dt

import pytest

from micronalgo.calendar_nyse import (
    Calendar,
    CalendarError,
    ExchangeCalendarsSource,
    RuleSource,
    Session,
    _easter,
    _observed,
)
from micronalgo.config import Settings


@pytest.mark.parametrize(
    "day,expect_open,expect_close",
    [
        (dt.date(2024, 7, 3), True, dt.time(13, 0)),     # day before Independence Day
        (dt.date(2024, 11, 29), True, dt.time(13, 0)),   # day after Thanksgiving
        (dt.date(2024, 12, 24), True, dt.time(13, 0)),   # Christmas Eve
        (dt.date(2024, 12, 25), False, None),            # Christmas
        (dt.date(2025, 1, 9), False, None),              # Carter funeral, ad hoc
        (dt.date(2022, 6, 20), False, None),             # Juneteenth observed
        (dt.date(2021, 7, 5), False, None),              # 4 July observed Monday
        (dt.date(2021, 7, 2), True, dt.time(16, 0)),     # ... so 2 July is a full day
        (dt.date(2026, 7, 3), False, None),              # 4 July 2026 is a Saturday
        (dt.date(2015, 4, 3), False, None),              # Good Friday
        (dt.date(2012, 10, 30), False, None),            # Hurricane Sandy
    ],
)
def test_known_sessions(day, expect_open, expect_close, rule_calendar, strict_calendar):
    for cal in (rule_calendar, strict_calendar):
        s = cal.session(day)
        if not expect_open:
            assert s is None, f"{cal.authority} thinks {day} is open"
        else:
            assert s is not None, f"{cal.authority} thinks {day} is closed"
            assert s.close_time == expect_close


def test_rule_engine_matches_exchange_calendars_over_decades(rule_calendar, strict_calendar):
    """A 36-year full sweep, not a spot check."""
    day, end = dt.date(1994, 1, 1), dt.date(2030, 12, 31)
    mismatches = []
    n_sessions = n_early = 0
    while day <= end:
        try:
            truth = strict_calendar.session(day)
        except CalendarError:
            day += dt.timedelta(days=1)
            continue
        mine = rule_calendar.session(day)
        if truth is not None:
            n_sessions += 1
            n_early += truth.is_early_close
        a = None if mine is None else (mine.open_time, mine.close_time)
        b = None if truth is None else (truth.open_time, truth.close_time)
        if a != b:
            mismatches.append((day, a, b))
        day += dt.timedelta(days=1)

    # exchange_calendars only generates a rolling window (roughly 20 years back
    # and one year forward), so the sweep is bounded by what it can answer --
    # still several thousand sessions and every scheduled early close in them.
    assert n_sessions > 4000, f"sweep covered only {n_sessions} sessions"
    assert n_early > 30, f"sweep covered only {n_early} early closes"
    assert mismatches == [], f"{len(mismatches)} disagreements, first 5: {mismatches[:5]}"


def test_new_years_day_on_saturday_is_not_observed():
    """The NYSE trades 31 December instead -- unlike most fixed-date holidays."""
    assert _observed(dt.date(2022, 1, 1)) is None
    assert _observed(dt.date(2021, 12, 25)) == dt.date(2021, 12, 24)


@pytest.mark.parametrize("year,expected", [
    (2024, dt.date(2024, 3, 31)), (2025, dt.date(2025, 4, 20)), (2000, dt.date(2000, 4, 23)),
])
def test_easter_algorithm(year, expected):
    assert _easter(year) == expected


def test_strict_mode_refuses_to_guess():
    """Trading is the risky act; an unresolved calendar must block it."""
    cal = Calendar([RuleSource()], strict=True)
    with pytest.raises(CalendarError, match="non-authoritative"):
        cal.session(dt.date(2025, 6, 10))


def test_no_source_raises_rather_than_returning_none():
    cal = Calendar([], strict=False)
    with pytest.raises(CalendarError):
        cal.session(dt.date(2025, 6, 10))


def test_broker_calendar_takes_priority():
    from micronalgo.calendar_nyse import BrokerCalendarSource

    fake = {dt.date(2025, 6, 10): (dt.time(9, 30), dt.time(11, 0))}
    cal = Calendar([BrokerCalendarSource(fake), ExchangeCalendarsSource()], strict=True)
    assert cal.session(dt.date(2025, 6, 10)).close_time == dt.time(11, 0)
    # Outside the broker's range it falls through to the next source.
    assert cal.session(dt.date(2025, 6, 11)).close_time == dt.time(16, 0)


def test_dst_is_handled_by_using_local_wall_clock():
    """09:30 New York is 13:30 UTC in winter and 14:30 UTC in summer."""
    winter = Session(dt.date(2025, 1, 15), dt.time(9, 30), dt.time(16, 0)).open_dt()
    summer = Session(dt.date(2025, 7, 15), dt.time(9, 30), dt.time(16, 0)).open_dt()
    assert winter.utcoffset() == dt.timedelta(hours=-5)
    assert summer.utcoffset() == dt.timedelta(hours=-4)
    assert winter.hour == summer.hour == 9


def test_submission_windows_shift_on_half_days(strict_calendar):
    """The single most important operational consequence of the calendar."""
    s = Settings()
    regular = strict_calendar.session(dt.date(2025, 6, 10))
    half = strict_calendar.session(dt.date(2024, 11, 29))

    r_submit, r_cutoff = s.entry_window(regular.close_dt())
    h_submit, h_cutoff = s.entry_window(half.close_dt())

    assert (r_submit.hour, r_submit.minute) == (15, 45)
    assert (h_submit.hour, h_submit.minute) == (12, 45)
    assert r_cutoff > r_submit and h_cutoff > h_submit
    assert h_cutoff < half.close_dt()


def test_next_and_previous_session_skip_holidays(strict_calendar):
    # 2024-11-28 is Thanksgiving.
    assert strict_calendar.next_session(dt.date(2024, 11, 27)).date == dt.date(2024, 11, 29)
    assert strict_calendar.previous_session(dt.date(2024, 11, 29)).date == dt.date(2024, 11, 27)
