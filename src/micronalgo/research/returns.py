"""Overnight / intraday return decomposition.

The whole project rests on one exact algebraic identity. For a session ``t``
with open ``O_t`` and close ``C_t``:

    r_on(t)  = O_t / C_{t-1} - 1        (close -> next open, "overnight")
    r_id(t)  = C_t / O_t     - 1        (open  -> same close, "intraday")
    r_cc(t)  = C_t / C_{t-1} - 1        (close -> close, what buy & hold earns)

    (1 + r_cc) == (1 + r_on) * (1 + r_id)          [exact, by cancellation of O_t]

Every claim about "the overnight effect" is a statement about how the *product*
of a stock's total return splits across these two disjoint, exhaustive windows.

Two traps this module exists to close:

1. **Summation instead of compounding.** ``sum(r_on)`` is not the return of the
   strategy. Compounding is ``prod(1 + r_on) - 1``. On a series with daily
   sigma ~3 % the two differ by orders of magnitude.
2. **Mismatched sources.** :func:`identity_error` guards the arithmetic. Note
   carefully what it can and cannot do: when all three returns are derived from
   the *same* two columns the identity holds algebraically no matter how badly
   the series is adjusted, because ``O_t`` cancels. It therefore catches a frame
   stitched together from different providers, or a vendor-supplied return
   column that disagrees with the prices -- but it is **not** a split detector.
   Detecting mis-adjustment needs a different test, and
   :func:`micronalgo.data.validate.check_adjustment` is that test.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "Decomposition",
    "decompose",
    "identity_error",
    "compound",
    "equity_curve",
    "to_log",
    "overnight_calendar_days",
]


@dataclass(frozen=True)
class Decomposition:
    """Per-session return decomposition plus provenance."""

    frame: pd.DataFrame
    max_identity_error: float
    n_sessions: int

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"Decomposition(n={self.n_sessions}, "
            f"max_identity_error={self.max_identity_error:.3e})"
        )


def decompose(df: pd.DataFrame, *, price_cols: tuple[str, str] = ("open", "close")) -> Decomposition:
    """Split each session's return into its overnight and intraday components.

    Parameters
    ----------
    df:
        Canonical bar frame (see :mod:`micronalgo.data.schema`), sorted ascending.
    price_cols:
        ``(open_col, close_col)``. Defaults to the adjusted columns. Pass
        ``("raw_open", "raw_close")`` to see what an *unadjusted* series would
        have produced -- useful for demonstrating split artefacts.

    Returns
    -------
    Decomposition
        ``frame`` has columns ``r_on``, ``r_id``, ``r_cc``, ``prev_close``,
        ``gap_days``. The first session is dropped: it has no previous close, so
        its overnight return is undefined. Silently keeping it as 0.0 would add a
        free session to the strategy and is a classic off-by-one.
    """
    open_col, close_col = price_cols
    if open_col not in df.columns or close_col not in df.columns:
        raise KeyError(f"frame lacks {open_col!r}/{close_col!r}")
    if not df.index.is_monotonic_increasing:
        raise ValueError("frame must be sorted ascending before decomposition")

    o = df[open_col].astype("float64")
    c = df[close_col].astype("float64")
    prev_c = c.shift(1)

    with np.errstate(divide="ignore", invalid="ignore"):
        r_on = o / prev_c - 1.0
        r_id = c / o - 1.0
        r_cc = c / prev_c - 1.0

    out = pd.DataFrame(
        {
            "r_on": r_on,
            "r_id": r_id,
            "r_cc": r_cc,
            "prev_close": prev_c,
            "open": o,
            "close": c,
        }
    )
    out["gap_days"] = overnight_calendar_days(df.index)
    out = out.iloc[1:]  # first session has no previous close

    finite = np.isfinite(out["r_on"]) & np.isfinite(out["r_id"]) & np.isfinite(out["r_cc"])
    err = identity_error(out.loc[finite])
    return Decomposition(frame=out, max_identity_error=err, n_sessions=int(finite.sum()))


def identity_error(frame: pd.DataFrame) -> float:
    """Max absolute violation of ``(1+r_cc) == (1+r_on)*(1+r_id)``.

    For returns derived from a single consistent pair of columns this is at
    float64 round-off level (< 1e-12) *by construction* -- ``O_t`` cancels. A
    non-zero value therefore means the three series do not describe the same
    price path: mismatched providers, a vendor return column that disagrees with
    the prices, or a bug in this package. It is a wiring check, not an
    adjustment check.
    """
    if frame.empty:
        return 0.0
    lhs = (1.0 + frame["r_cc"]).to_numpy()
    rhs = ((1.0 + frame["r_on"]) * (1.0 + frame["r_id"])).to_numpy()
    mask = np.isfinite(lhs) & np.isfinite(rhs)
    if not mask.any():
        return 0.0
    return float(np.nanmax(np.abs(lhs[mask] - rhs[mask])))


def overnight_calendar_days(index: pd.DatetimeIndex) -> pd.Series:
    """Calendar days spanned by each session's overnight window.

    1 for Tue->Wed, 3 for Fri->Mon, 4 across a Monday holiday. The overnight
    "premium" is often reported per *session*; if it were compensation for
    calendar-time risk it should scale with this number. It does not, which is
    itself evidence about the nature of the effect -- so we keep the column.
    """
    days = pd.Series(index, index=index).diff().dt.days
    return days.astype("float64")


def compound(returns: pd.Series | np.ndarray) -> float:
    """Total compounded return: ``prod(1+r) - 1``, NaNs treated as flat (0)."""
    arr = np.asarray(returns, dtype="float64")
    arr = np.where(np.isfinite(arr), arr, 0.0)
    if arr.size == 0:
        return 0.0
    # Products of ~10k terms underflow/overflow in naive form; go through logs
    # where possible and fall back to the direct product if any 1+r <= 0.
    gross = 1.0 + arr
    if np.any(gross <= 0.0):
        return float(np.prod(gross) - 1.0)
    return float(np.expm1(np.sum(np.log(gross))))


def equity_curve(returns: pd.Series, *, initial: float = 1.0) -> pd.Series:
    """Cumulative equity from a series of per-period simple returns.

    A ``-100 %`` period wipes the curve to zero and it stays there: ``cumprod``
    gives that for free and it is the correct behaviour (you cannot recover from
    a total loss). NaNs are treated as flat periods.
    """
    r = returns.astype("float64").fillna(0.0)
    curve = initial * (1.0 + r).cumprod()
    curve.name = returns.name if returns.name else "equity"
    return curve


def to_log(returns: pd.Series) -> pd.Series:
    """Log returns, with ``1+r <= 0`` mapped to ``-inf`` rather than NaN."""
    gross = 1.0 + returns.astype("float64")
    return pd.Series(
        np.where(gross > 0, np.log(np.where(gross > 0, gross, 1.0)), -np.inf),
        index=returns.index,
        name=f"log_{returns.name}" if returns.name else "log_ret",
    )
