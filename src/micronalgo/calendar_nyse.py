"""NYSE session calendar with a fail-closed source hierarchy.

Why this module is more than ``pandas.bdate_range``
---------------------------------------------------
The strategy submits a market-on-close order a few minutes before the closing
auction and a market-on-open order before the opening auction. Two calendar
mistakes are catastrophic rather than cosmetic:

* **Early close (13:00 ET).** On ~5 sessions a year the NYSE closes at 13:00.
  A scheduler that wakes at 15:50 ET finds the market shut, the MOC order is
  never accepted (or worse, queues for the *next* session), and the position is
  either missing or held for an extra full day of intraday risk -- the exact
  exposure the strategy exists to avoid.
* **DST.** 09:30 America/New_York is 13:30 UTC in winter and 14:30 UTC in
  summer. Any schedule expressed in UTC is wrong for half the year. Every time
  in this project is therefore an America/New_York wall-clock time.

Source hierarchy (highest authority first)
------------------------------------------
1. **Broker calendar** (Alpaca ``/v2/calendar``) -- authoritative, includes
   ad-hoc closures and early closes, and is what the broker itself will act on.
2. ``exchange_calendars`` XNYS -- well maintained, offline, knows historical
   special closes.
3. **Built-in rule engine** -- US federal/NYSE holiday rules plus an explicit
   table of ad-hoc closures. Good enough for research, and marked as such.

In :data:`STRICT` mode (the default for live trading) an unresolved session
raises instead of guessing. Trading is the risky action; refusing to trade is
the safe one, so ambiguity must resolve to "do nothing".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")

REGULAR_OPEN = dt.time(9, 30)
REGULAR_CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)

STRICT = True


class CalendarError(RuntimeError):
    """Raised when the trading calendar cannot be resolved authoritatively."""


@dataclass(frozen=True)
class Session:
    """One trading session in exchange local time."""

    date: dt.date
    open_time: dt.time
    close_time: dt.time

    @property
    def is_early_close(self) -> bool:
        return self.close_time < REGULAR_CLOSE

    def open_dt(self) -> dt.datetime:
        return dt.datetime.combine(self.date, self.open_time, tzinfo=NY)

    def close_dt(self) -> dt.datetime:
        return dt.datetime.combine(self.date, self.close_time, tzinfo=NY)


class SessionSource(Protocol):
    """Anything that can answer 'is this date a session and when does it close?'."""

    name: str

    def session(self, day: dt.date) -> Session | None:
        """Return the :class:`Session` for ``day``, or ``None`` if the market is closed.

        Raise :class:`CalendarError` if the source cannot answer for this date
        (e.g. outside its known range) so the caller can fall through.
        """
        ...


# --------------------------------------------------------------------------- #
# Rule engine (fallback)
# --------------------------------------------------------------------------- #

# Ad-hoc, non-rule-based NYSE closures. Not exhaustive before 1985; the rule
# engine is a *fallback* and says so via `Calendar.authority`.
AD_HOC_CLOSURES: frozenset[dt.date] = frozenset(
    {
        dt.date(1985, 9, 27),  # Hurricane Gloria
        dt.date(1994, 4, 27),  # Nixon funeral
        dt.date(2001, 9, 11),  # 9/11
        dt.date(2001, 9, 12),
        dt.date(2001, 9, 13),
        dt.date(2001, 9, 14),
        dt.date(2004, 6, 11),  # Reagan funeral
        dt.date(2007, 1, 2),   # Ford funeral
        dt.date(2012, 10, 29), # Hurricane Sandy
        dt.date(2012, 10, 30),
        dt.date(2018, 12, 5),  # G.H.W. Bush funeral
        dt.date(2025, 1, 9),   # Carter funeral
    }
)

# Ad-hoc early closes that no rule generates.
AD_HOC_EARLY_CLOSES: frozenset[dt.date] = frozenset(
    {
        dt.date(2001, 9, 17),
        dt.date(2001, 9, 18),
        dt.date(2001, 9, 19),
        dt.date(2001, 9, 20),
        dt.date(2001, 9, 21),
    }
)


def _easter(year: int) -> dt.date:
    """Gregorian Easter Sunday (Anonymous Gregorian / Meeus algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """``n``-th ``weekday`` (Mon=0) of ``month``; ``n=-1`` means the last one."""
    if n > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (n - 1))
    last_day = (dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1))
    offset = (last_day.weekday() - weekday) % 7
    return last_day - dt.timedelta(days=offset)


def _observed(day: dt.date) -> dt.date | None:
    """NYSE observation rule for fixed-date holidays.

    Saturday -> observed the preceding Friday; Sunday -> the following Monday.
    Exception: a Saturday **New Year's Day** is *not* observed -- the NYSE trades
    the preceding 31 December as a normal session.
    """
    if day.weekday() == 5:  # Saturday
        if day.month == 1 and day.day == 1:
            return None
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:  # Sunday
        return day + dt.timedelta(days=1)
    return day


@lru_cache(maxsize=256)
def _rule_holidays(year: int) -> frozenset[dt.date]:
    out: set[dt.date] = set()

    def add(d: dt.date | None) -> None:
        if d is not None:
            out.add(d)

    add(_observed(dt.date(year, 1, 1)))                       # New Year's Day
    if year >= 1998:
        add(_nth_weekday(year, 1, 0, 3))                      # MLK Day
    if year >= 1971:
        add(_nth_weekday(year, 2, 0, 3))                      # Washington's Birthday
    else:
        add(_observed(dt.date(year, 2, 22)))
    add(_easter(year) - dt.timedelta(days=2))                 # Good Friday
    if year >= 1971:
        add(_nth_weekday(year, 5, 0, -1))                     # Memorial Day
    else:
        add(_observed(dt.date(year, 5, 30)))
    if year >= 2022:
        add(_observed(dt.date(year, 6, 19)))                  # Juneteenth
    add(_observed(dt.date(year, 7, 4)))                       # Independence Day
    add(_nth_weekday(year, 9, 0, 1))                          # Labor Day
    add(_nth_weekday(year, 11, 3, 4))                         # Thanksgiving
    add(_observed(dt.date(year, 12, 25)))                     # Christmas
    return frozenset(out)


@lru_cache(maxsize=256)
def _rule_early_closes(year: int) -> frozenset[dt.date]:
    """Scheduled 13:00 ET closes.

    * Friday after Thanksgiving (since 1993).
    * 3 July, when it is a weekday **and** is not itself the observed holiday.
    * 24 December, when it is a weekday **and** is not the observed holiday.
    """
    out: set[dt.date] = set()
    holidays = _rule_holidays(year)

    if year >= 1993:
        out.add(_nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1))

    jul3 = dt.date(year, 7, 3)
    if jul3.weekday() < 5 and jul3 not in holidays:
        out.add(jul3)

    dec24 = dt.date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in holidays:
        out.add(dec24)

    return frozenset(out - holidays)


class RuleSource:
    """Offline rule-based session source. Advisory authority only."""

    name = "rules"
    authoritative = False

    def session(self, day: dt.date) -> Session | None:
        if day.weekday() >= 5:
            return None
        if day in AD_HOC_CLOSURES or day in _rule_holidays(day.year):
            return None
        early = day in _rule_early_closes(day.year) or day in AD_HOC_EARLY_CLOSES
        return Session(day, REGULAR_OPEN, EARLY_CLOSE if early else REGULAR_CLOSE)


class ExchangeCalendarsSource:
    """Session source backed by the ``exchange_calendars`` XNYS calendar."""

    name = "exchange_calendars"
    authoritative = True

    def __init__(self) -> None:
        import exchange_calendars as xcals  # imported lazily: optional dependency

        self._cal = xcals.get_calendar("XNYS")

    def session(self, day: dt.date) -> Session | None:
        ts = pd.Timestamp(day)
        first, last = self._cal.first_session, self._cal.last_session
        if ts < first.tz_localize(None) if first.tz else ts < first:
            raise CalendarError(f"{day} precedes XNYS calendar start")
        bound = last.tz_localize(None) if last.tz else last
        if ts > bound:
            raise CalendarError(f"{day} beyond XNYS calendar end {bound.date()}")
        if not self._cal.is_session(ts):
            return None
        open_ny = self._cal.session_open(ts).tz_convert(NY)
        close_ny = self._cal.session_close(ts).tz_convert(NY)
        return Session(day, open_ny.time(), close_ny.time())


class BrokerCalendarSource:
    """Session source backed by a broker calendar mapping ``date -> (open, close)``.

    Populated by :mod:`micronalgo.live.alpaca` from ``/v2/calendar``. Highest
    authority: it is literally the schedule the broker will enforce.
    """

    name = "broker"
    authoritative = True

    def __init__(self, sessions: dict[dt.date, tuple[dt.time, dt.time]]) -> None:
        self._sessions = dict(sessions)
        self._min = min(self._sessions) if self._sessions else None
        self._max = max(self._sessions) if self._sessions else None

    def session(self, day: dt.date) -> Session | None:
        if self._min is None or not (self._min <= day <= self._max):
            raise CalendarError(f"{day} outside broker calendar range {self._min}..{self._max}")
        hit = self._sessions.get(day)
        if hit is None:
            return None
        return Session(day, hit[0], hit[1])


class Calendar:
    """Resolves sessions through an ordered list of sources."""

    def __init__(self, sources: list[SessionSource] | None = None, *, strict: bool = STRICT) -> None:
        self.strict = strict
        self.sources: list[SessionSource] = sources if sources is not None else default_sources()

    @property
    def authority(self) -> str:
        return ",".join(s.name for s in self.sources)

    def session(self, day: dt.date | dt.datetime | pd.Timestamp) -> Session | None:
        day = _as_date(day)
        errors: list[str] = []
        for src in self.sources:
            try:
                result = src.session(day)
            except CalendarError as exc:
                errors.append(f"{src.name}: {exc}")
                continue
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"{src.name}: unexpected {exc!r}")
                continue
            if self.strict and not getattr(src, "authoritative", False):
                raise CalendarError(
                    f"only non-authoritative source {src.name!r} could resolve {day}; "
                    "refusing to trade on a guessed calendar "
                    f"(prior failures: {errors or 'none'})"
                )
            return result
        raise CalendarError(f"no source could resolve session {day}: {errors}")

    def is_session(self, day: dt.date | dt.datetime | pd.Timestamp) -> bool:
        return self.session(day) is not None

    def next_session(self, after: dt.date | dt.datetime | pd.Timestamp, *, limit: int = 15) -> Session:
        day = _as_date(after)
        for _ in range(limit):
            day += dt.timedelta(days=1)
            s = self.session(day)
            if s is not None:
                return s
        raise CalendarError(f"no session found within {limit} days after {after}")

    def previous_session(self, before: dt.date | dt.datetime | pd.Timestamp, *, limit: int = 15) -> Session:
        day = _as_date(before)
        for _ in range(limit):
            day -= dt.timedelta(days=1)
            s = self.session(day)
            if s is not None:
                return s
        raise CalendarError(f"no session found within {limit} days before {before}")

    def sessions_between(self, start: dt.date, end: dt.date) -> list[Session]:
        out: list[Session] = []
        day = _as_date(start)
        stop = _as_date(end)
        while day <= stop:
            s = self.session(day)
            if s is not None:
                out.append(s)
            day += dt.timedelta(days=1)
        return out


def _as_date(value: dt.date | dt.datetime | pd.Timestamp) -> dt.date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, dt.datetime):
        return value.date()
    return value


def default_sources(*, allow_rules: bool = True) -> list[SessionSource]:
    """Best available offline sources, highest authority first."""
    sources: list[SessionSource] = []
    try:
        sources.append(ExchangeCalendarsSource())
    except Exception:  # pragma: no cover - optional dependency missing
        pass
    if allow_rules:
        sources.append(RuleSource())
    return sources


def research_calendar() -> Calendar:
    """Non-strict calendar for backtesting (guessing a 1994 half-day is harmless)."""
    return Calendar(strict=False)


def now_ny() -> dt.datetime:
    """Current time as an America/New_York aware datetime."""
    return dt.datetime.now(tz=NY)
