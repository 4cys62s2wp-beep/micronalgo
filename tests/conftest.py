"""Shared fixtures.

No test in this suite touches the network. Every price series is either
synthetic with an analytically known answer, or a recorded provider payload.
"""

from __future__ import annotations

import datetime as dt

import pytest

from micronalgo.calendar_nyse import Calendar, ExchangeCalendarsSource, RuleSource
from micronalgo.config import Settings
from micronalgo.data.synthetic import constant_series, random_walk


@pytest.fixture
def rule_calendar() -> Calendar:
    return Calendar([RuleSource()], strict=False)


@pytest.fixture
def strict_calendar() -> Calendar:
    return Calendar([ExchangeCalendarsSource()], strict=True)


@pytest.fixture
def flat_bars():
    """500 sessions of exactly +10 bps overnight and -5 bps intraday."""
    return constant_series(500, r_on=0.0010, r_id=-0.0005)


@pytest.fixture
def walk_bars():
    return random_walk(1500, seed=20240824, start="2015-01-02")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        broker="sim",
        dry_run=False,
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
        reports_dir=tmp_path / "reports",
        kill_switch_file=tmp_path / "state" / "KILL",
        fractional_shares=False,
        max_data_age_days=100_000,
    )


@pytest.fixture
def session_dates(rule_calendar):
    def _dates(start: dt.date, n: int) -> list[dt.date]:
        out, day = [], start
        while len(out) < n:
            if rule_calendar.is_session(day):
                out.append(day)
            day += dt.timedelta(days=1)
        return out
    return _dates
