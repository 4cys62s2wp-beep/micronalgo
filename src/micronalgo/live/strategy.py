"""Strategy decision layer -- the single definition of what the bot wants to do.

The backtest and the live bot must not be allowed to drift apart, so both size
positions through :func:`target_shares` and both express the strategy as the
same statement: *hold the symbol from each session's close to the next
session's open, and hold nothing else, ever*.

There is no signal to compute in the base strategy. That is not a simplification
-- it is the hypothesis. Any filter is an optional overlay, and the report
measures the overlay against the unfiltered baseline so that added complexity
has to earn its place.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import pandas as pd

from ..config import Settings
from ..research import filters as F


@dataclass(frozen=True)
class SizingDecision:
    shares: float
    notional: float
    reference_price: float
    reason: str = ""

    @property
    def tradable(self) -> bool:
        return self.shares > 0


def target_shares(
    equity: float,
    reference_price: float,
    settings: Settings,
    *,
    adv: float | None = None,
) -> SizingDecision:
    """How many shares to buy into the closing auction.

    Sizing is deliberately conservative in three independent ways, because each
    guards a different failure:

    * ``capital_fraction`` < 1 leaves headroom so a fill above the reference
      price cannot reject the order for insufficient buying power;
    * ``max_notional`` / ``max_shares`` cap a configuration mistake;
    * ``max_participation_of_adv`` caps market impact, which matters not because
      retail size moves MU but because a misplaced decimal would.
    """
    if reference_price <= 0 or equity <= 0:
        return SizingDecision(0.0, 0.0, reference_price, "non-positive equity or price")

    notional = equity * settings.capital_fraction * settings.leverage
    notional = min(notional, settings.max_notional)
    shares = notional / reference_price

    reasons: list[str] = []
    if adv and adv > 0:
        cap = adv * settings.max_participation_of_adv
        if shares > cap:
            shares = cap
            reasons.append(f"capped at {settings.max_participation_of_adv:.2%} of ADV")

    if shares > settings.max_shares:
        shares = float(settings.max_shares)
        reasons.append("capped at max_shares")

    shares = shares if settings.fractional_shares else float(math.floor(shares))
    if shares <= 0:
        return SizingDecision(0.0, 0.0, reference_price, "position rounds to zero shares")

    return SizingDecision(shares, shares * reference_price, reference_price, "; ".join(reasons))


def should_trade_session(
    settings: Settings,
    bars: pd.DataFrame | None,
    entry_date: dt.date,
) -> tuple[bool, str]:
    """Optional overlay filters, evaluated with data available before the close.

    Returns ``(allow, reason)``. The base strategy trades every session, so this
    returns ``True`` unless a filter is explicitly enabled in the settings.
    """
    if not settings.skip_earnings:
        return True, ""
    dates = F.load_earnings_dates(settings.earnings_csv)
    if not dates:
        return True, (
            f"skip_earnings is on but {settings.earnings_csv} is empty or missing; "
            "trading anyway rather than silently applying a filter that does nothing"
        )
    window = settings.earnings_blackout_sessions
    if bars is not None and window > 0:
        idx = bars.index
        pos = idx.searchsorted(pd.Timestamp(entry_date))
        for d in dates:
            dpos = idx.searchsorted(pd.Timestamp(d))
            if abs(int(pos) - int(dpos)) <= window:
                return False, f"within {window} sessions of the earnings date {d}"
    elif entry_date in set(dates):
        return False, f"earnings reported after the close on {entry_date}"
    return True, ""
