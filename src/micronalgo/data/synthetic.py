"""Synthetic OHLC generation with analytically known decomposition.

The build environment has no access to any market-data host, so engine
correctness is established the way it should be established anyway: against
series whose true answer is known in closed form rather than against a
downloaded file nobody re-derives.

:func:`from_returns` is the workhorse -- you hand it the overnight and intraday
return sequences you want, and it constructs the OHLC frame that produces
exactly those. The expected equity curve is then ``cumprod(1 + r)`` with no
estimation involved, so any engine discrepancy is a bug rather than noise.

:func:`inject_split` and :func:`break_open_adjustment` manufacture the two
data-quality failures that would otherwise silently invalidate the whole study,
so the validator can be tested against real instances of them.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ..calendar_nyse import Calendar, RuleSource
from .schema import coerce

_RESEARCH_CAL = Calendar([RuleSource()], strict=False)


def session_index(start: str | dt.date, n: int) -> pd.DatetimeIndex:
    """``n`` real NYSE session dates starting at or after ``start``."""
    day = pd.Timestamp(start).date()
    out: list[dt.date] = []
    guard = 0
    while len(out) < n and guard < n * 4 + 64:
        if _RESEARCH_CAL.is_session(day):
            out.append(day)
        day += dt.timedelta(days=1)
        guard += 1
    return pd.DatetimeIndex(pd.to_datetime(out), name="date")


def from_returns(
    r_on: np.ndarray | list[float],
    r_id: np.ndarray | list[float],
    *,
    start: str | dt.date = "2000-01-03",
    initial_close: float = 100.0,
    volume: float = 20_000_000.0,
    range_pad: float = 0.004,
) -> pd.DataFrame:
    """Build a canonical bar frame realising exactly the given returns.

    The first row is a seed session carrying only ``initial_close``; the first
    *usable* session is row 1, matching ``r_on[0]`` / ``r_id[0]``. That mirrors
    the engine, which also discards the first row for lack of a previous close.
    """
    on = np.asarray(r_on, dtype="float64")
    idr = np.asarray(r_id, dtype="float64")
    if on.shape != idr.shape:
        raise ValueError(f"r_on and r_id must have equal length, got {on.shape} vs {idr.shape}")
    n = on.size

    closes = np.empty(n + 1, dtype="float64")
    opens = np.empty(n + 1, dtype="float64")
    closes[0] = initial_close
    opens[0] = initial_close
    for i in range(n):
        opens[i + 1] = closes[i] * (1.0 + on[i])
        closes[i + 1] = opens[i + 1] * (1.0 + idr[i])

    hi = np.maximum(opens, closes) * (1.0 + range_pad)
    lo = np.minimum(opens, closes) * (1.0 - range_pad)

    idx = session_index(start, n + 1)
    df = pd.DataFrame(
        {
            "open": opens,
            "high": hi,
            "low": lo,
            "close": closes,
            "raw_open": opens,
            "raw_high": hi,
            "raw_low": lo,
            "raw_close": closes,
            "volume": np.full(n + 1, float(volume)),
            "adj_factor": np.ones(n + 1),
        },
        index=idx,
    )
    return coerce(df)


def random_walk(
    n: int = 2000,
    *,
    overnight_mu: float = 0.0006,
    overnight_sigma: float = 0.018,
    intraday_mu: float = -0.0004,
    intraday_sigma: float = 0.020,
    seed: int = 12345,
    start: str = "2000-01-03",
    fat_tail_df: float | None = 4.0,
    initial_close: float = 100.0,
) -> pd.DataFrame:
    """A MU-flavoured synthetic series: positive overnight drift, negative intraday.

    ``fat_tail_df`` uses a Student-t (scaled to the requested sigma) so the
    left tail resembles a stock that gaps on earnings, which matters for every
    risk metric in the report.
    """
    rng = np.random.default_rng(seed)

    def draw(mu: float, sigma: float) -> np.ndarray:
        if fat_tail_df is None:
            return rng.normal(mu, sigma, n)
        raw = rng.standard_t(fat_tail_df, n)
        raw = raw / np.sqrt(fat_tail_df / (fat_tail_df - 2.0))
        return mu + sigma * raw

    return from_returns(
        draw(overnight_mu, overnight_sigma),
        draw(intraday_mu, intraday_sigma),
        start=start,
        initial_close=initial_close,
    )


def inject_split(df: pd.DataFrame, on: str | dt.date, ratio: float = 2.0) -> pd.DataFrame:
    """Return a copy whose **raw** prices are un-adjusted for a split on ``on``.

    All raw prices strictly before the ex-date are multiplied by ``ratio``,
    exactly as an unadjusted vendor feed would show them, while the adjusted
    columns stay clean. Feeding the raw columns to the decomposition then
    produces the textbook artefact: a fake ``-50 %`` overnight return on the
    ex-date. Used to test the split detector.
    """
    out = df.copy()
    ex = pd.Timestamp(on).normalize()
    mask = out.index < ex
    for c in ("raw_open", "raw_high", "raw_low", "raw_close"):
        out.loc[mask, c] = out.loc[mask, c] * ratio
    out.loc[mask, "adj_factor"] = out.loc[mask, "adj_factor"] / ratio
    return out


def break_open_adjustment(df: pd.DataFrame, on: str | dt.date, ratio: float = 2.0) -> pd.DataFrame:
    """Adjust ``close`` for a split but leave ``open`` unadjusted.

    This is the *dangerous* corruption: unlike a fully unadjusted series, the
    close-to-close curve still looks perfectly normal, so buy & hold validates
    fine while the overnight/intraday split is destroyed. Detected only by the
    identity check, which is why that check is mandatory.
    """
    out = df.copy()
    ex = pd.Timestamp(on).normalize()
    mask = out.index < ex
    out.loc[mask, "open"] = out.loc[mask, "open"] * ratio
    out.loc[mask, "raw_open"] = out.loc[mask, "raw_open"] * ratio
    return out


def constant_series(
    n: int, *, r_on: float, r_id: float, start: str = "2000-01-03", initial_close: float = 100.0
) -> pd.DataFrame:
    """Deterministic series with constant per-session returns.

    The expected compounded result is exactly ``(1+r)**n - 1``, which makes
    every assertion in the engine tests a closed-form comparison.
    """
    return from_returns(
        np.full(n, r_on), np.full(n, r_id), start=start, initial_close=initial_close
    )
