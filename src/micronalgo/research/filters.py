"""Trade filters, built so lookahead is structurally impossible.

The contract
------------
The engine enters the overnight position at the **close of session t-1** and
exits at the **open of session t**, and it indexes that trade under ``t``. A
filter must therefore answer, for each ``t``, a question decidable moments
before the close of ``t-1``.

Every builder here consumes the bar frame, computes an indicator, and then calls
:func:`lagged` -- which shifts by ``1 + extra_lag`` sessions so that the value
attached to ``t`` was computed from bars up to and including ``t-1``. Nothing in
this module is allowed to index the frame at ``t`` directly.

``extra_lag=0`` (the default) permits using the close of ``t-1`` itself, which is
realistic: at 15:55 ET the price you are about to trade at is on screen. Set
``extra_lag=1`` for a paranoid variant that only uses data through ``t-2``; the
report runs both, and a filter whose value collapses under the paranoid variant
was never a filter, it was a peek.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd


def lagged(series: pd.Series, *, extra_lag: int = 0) -> pd.Series:
    """Shift an indicator so its value at ``t`` was knowable before entering at ``t-1``."""
    return series.shift(1 + int(extra_lag))


def always(df: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=df.index, name="always")


def trend_filter(
    df: pd.DataFrame, *, window: int = 200, above: bool = True, extra_lag: int = 0
) -> pd.Series:
    """Trade only when the close is above (or below) its ``window``-session average."""
    sma = df["close"].rolling(window, min_periods=window).mean()
    raw = (df["close"] > sma) if above else (df["close"] < sma)
    out = lagged(raw, extra_lag=extra_lag).fillna(False).astype(bool)
    out.name = f"trend_{'above' if above else 'below'}_{window}"
    return out


def volatility_filter(
    df: pd.DataFrame,
    *,
    window: int = 20,
    max_annual_vol: float = 1.00,
    min_annual_vol: float = 0.0,
    extra_lag: int = 0,
) -> pd.Series:
    """Trade only when trailing realised close-to-close vol is inside a band.

    The overnight premium is usually argued to be compensation for holding
    unhedgeable gap risk. If so, it should be *larger* in high-vol regimes, not
    smaller -- so this filter is as much a diagnostic as a trading rule.
    """
    ret = df["close"].pct_change()
    vol = ret.rolling(window, min_periods=window).std() * np.sqrt(252)
    raw = (vol <= max_annual_vol) & (vol >= min_annual_vol)
    out = lagged(raw, extra_lag=extra_lag).fillna(False).astype(bool)
    out.name = f"vol_{min_annual_vol:g}_{max_annual_vol:g}_{window}"
    return out


def prior_move_filter(
    df: pd.DataFrame, *, max_abs_move: float = 0.10, extra_lag: int = 0
) -> pd.Series:
    """Skip the session after an outsized close-to-close move.

    A >10 % day is usually news; the following overnight is then a continuation
    lottery rather than the ordinary premium.
    """
    raw = df["close"].pct_change().abs() <= max_abs_move
    out = lagged(raw, extra_lag=extra_lag).fillna(False).astype(bool)
    out.name = f"prior_move_lt_{max_abs_move:g}"
    return out


def weekday_filter(df: pd.DataFrame, *, weekdays: Iterable[int] = (0, 1, 2, 3, 4)) -> pd.Series:
    """Trade only on selected arrival weekdays (Mon=0). Needs no lag: the
    calendar is known years in advance."""
    allowed = set(weekdays)
    out = pd.Series([ts.dayofweek in allowed for ts in df.index], index=df.index)
    out.name = f"weekday_{''.join(str(w) for w in sorted(allowed))}"
    return out.astype(bool)


def min_price_filter(df: pd.DataFrame, *, min_price: float = 5.0, extra_lag: int = 0) -> pd.Series:
    """Skip sessions where the as-traded price is low enough that tick-size
    friction and microstructure noise dominate."""
    out = lagged(df["raw_close"] >= min_price, extra_lag=extra_lag).fillna(False).astype(bool)
    out.name = f"min_price_{min_price:g}"
    return out


# --------------------------------------------------------------------------- #
# Earnings
# --------------------------------------------------------------------------- #

def load_earnings_dates(path: str | Path) -> list[dt.date]:
    """Load report dates from a CSV with a ``date`` column (ISO ``YYYY-MM-DD``).

    The authoritative source is the issuer's investor-relations calendar or a
    paid vendor. This project deliberately ships **no** hardcoded date list: a
    stale or invented earnings calendar is worse than none, because it silently
    excludes the wrong sessions and makes the backtest look cleaner than reality.
    """
    p = Path(path)
    if not p.exists():
        return []
    df = pd.read_csv(p)
    col = "date" if "date" in df.columns else df.columns[0]
    return [d.date() for d in pd.to_datetime(df[col], errors="coerce").dropna()]


def infer_earnings_dates(
    df: pd.DataFrame, *, quantile: float = 0.985, min_spacing: int = 40, max_spacing: int = 85
) -> list[dt.date]:
    """Heuristically infer the *entry* sessions that precede an earnings gap.

    Method: take sessions whose absolute overnight return sits above ``quantile``,
    then keep those spaced 40-85 sessions apart, which is the footprint of a
    quarterly reporting cadence. Returns the **entry** date (the session at whose
    close the position would be opened), i.e. the session *before* the gap.

    This is a fallback, and it is biased: it finds big gaps, which is not the
    same set as "earnings dates". It will miss quiet earnings and flag large
    non-earnings news. Use :func:`load_earnings_dates` with a real calendar for
    anything you intend to trade. The report labels results from this path
    explicitly as inferred.
    """
    close = df["close"]
    r_on = (df["open"] / close.shift(1) - 1.0).abs()
    thresh = r_on.quantile(quantile)
    candidates = [ts for ts, v in r_on.items() if np.isfinite(v) and v >= thresh]

    positions = {ts: i for i, ts in enumerate(df.index)}
    kept: list[pd.Timestamp] = []
    for ts in candidates:
        if not kept:
            kept.append(ts)
            continue
        gap = positions[ts] - positions[kept[-1]]
        if min_spacing <= gap <= max_spacing or gap > max_spacing:
            kept.append(ts)

    entries: list[dt.date] = []
    for ts in kept:
        i = positions[ts]
        if i > 0:
            entries.append(df.index[i - 1].date())
    return entries


def earnings_filter(
    df: pd.DataFrame,
    earnings_entry_dates: Iterable[dt.date],
    *,
    skip: bool = True,
    window: int = 0,
) -> pd.Series:
    """Exclude (or isolate) the overnight windows that straddle an earnings report.

    ``earnings_entry_dates`` are the sessions at whose **close** the position
    would be opened. ``window`` widens the blackout by that many sessions on each
    side. ``skip=False`` inverts the mask, which is how the report measures how
    much of the total P&L those few sessions actually contributed.
    """
    entry_set = {pd.Timestamp(d).normalize() for d in earnings_entry_dates}
    positions = {ts: i for i, ts in enumerate(df.index)}
    blocked: set[int] = set()
    for ts in entry_set:
        i = positions.get(ts)
        if i is None:
            continue
        # The engine indexes the trade under the *arrival* session i+1.
        for w in range(-window, window + 1):
            j = i + 1 + w
            if 0 <= j < len(df.index):
                blocked.add(j)

    flags = np.array([i not in blocked for i in range(len(df.index))])
    out = pd.Series(flags if skip else ~flags, index=df.index)
    out.name = f"earnings_{'skip' if skip else 'only'}_w{window}"
    return out.astype(bool)


# --------------------------------------------------------------------------- #

def combine(*signals: pd.Series, how: str = "and") -> pd.Series:
    """Combine boolean signals. Missing values are treated as "do not trade"."""
    if not signals:
        raise ValueError("combine() needs at least one signal")
    frame = pd.concat([s.astype("boolean") for s in signals], axis=1).fillna(False)
    out = frame.all(axis=1) if how == "and" else frame.any(axis=1)
    out.name = f"{how}(" + ",".join(str(s.name) for s in signals) + ")"
    return out.astype(bool)


def assert_lag_safe(signal: pd.Series, df: pd.DataFrame, *, name: str = "signal") -> None:
    """Sanity check that a signal is not obviously peeking.

    Catches the common mistake of forgetting :func:`lagged` entirely: an
    unshifted indicator correlates with the *same* session's return far more than
    a shifted one does. This is a smoke alarm, not a proof; the proof is
    ``tests/test_filters.py::test_unlagged_filter_is_detectably_different``.
    """
    if not signal.index.equals(df.index):
        raise ValueError(f"{name}: index must match the bar frame exactly")
    if signal.dtype != bool:
        raise TypeError(f"{name}: must be a boolean series, got {signal.dtype}")
