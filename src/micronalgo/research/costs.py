"""Transaction-cost model.

Why costs decide this strategy
------------------------------
Buy-every-close / sell-every-open trades ~252 round trips a year. A cost of
``c`` per round trip compounds to ``(1-c)^252 - 1`` per year: 5 bps -> -1.25 %/yr,
25 bps -> -6.1 %/yr, 100 bps -> -92 %/yr. The measured overnight edge on a single
stock is of the order of a few basis points per session, so the cost assumption
is not a detail -- it *is* the result.

The one structural reason this strategy is not obviously dead
-------------------------------------------------------------
The natural execution is a **market-on-close** buy and a **market-on-open**
sell. Both fill at the official auction print. The daily bar's ``close`` *is*
the closing-auction price and its ``open`` *is* the opening-auction price. So a
retail-sized order does **not cross the spread** -- it is a price taker at a
price the backtest already uses. Slippage is therefore ~0 by construction, and
the residual costs are regulatory fees measured in fractions of a basis point.

That is a genuinely favourable structure, and it is also the single assumption
most worth attacking: see :func:`stress_grid` and the reality-check report.

The geometric trap
------------------
Break-even is set by the **geometric** mean, not the arithmetic mean. For daily
returns with mean ``mu`` and stdev ``sigma``,

    E[log(1+r)] ~= mu - sigma^2 / 2

MU's overnight sigma is ~2 %/session, so the variance drag is ~2 bps/session --
comparable to the entire edge. Any analysis that averages simple returns
overstates the strategy by roughly that amount every single day.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np
import pandas as pd

Side = Literal["buy", "sell"]

BPS = 1e-4


# --------------------------------------------------------------------------- #
# Regulatory fees (effective-dated, because these rates change and a hardcoded
# constant silently rots)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RegFeeTier:
    """SEC Section 31 / FINRA TAF rates effective from ``start`` (sell side only)."""

    start: dt.date
    sec_per_million: float   # USD per $1,000,000 of sell notional
    taf_per_share: float     # USD per share sold
    taf_cap: float           # USD max per trade


# Ordered ascending. Values are approximate historical levels and MUST be
# reviewed against the current SEC fee-rate advisory and FINRA Rule 7610A before
# any conclusion is drawn from a live P&L reconciliation.
# [verify-at-runtime: current-year rates]
REG_FEE_HISTORY: tuple[RegFeeTier, ...] = (
    RegFeeTier(dt.date(1994, 1, 1), 33.00, 0.000000, 0.00),
    RegFeeTier(dt.date(2003, 1, 1), 33.00, 0.000075, 30.00),
    RegFeeTier(dt.date(2008, 1, 1), 25.70, 0.000075, 30.00),
    RegFeeTier(dt.date(2012, 1, 1), 22.40, 0.000119, 5.95),
    RegFeeTier(dt.date(2016, 1, 1), 21.80, 0.000119, 5.95),
    RegFeeTier(dt.date(2019, 1, 1), 20.70, 0.000130, 6.50),
    RegFeeTier(dt.date(2021, 1, 1), 5.10, 0.000130, 6.50),
    RegFeeTier(dt.date(2022, 1, 1), 22.90, 0.000130, 6.50),
    RegFeeTier(dt.date(2023, 1, 1), 8.00, 0.000145, 7.27),
    RegFeeTier(dt.date(2024, 1, 1), 27.80, 0.000166, 8.30),
    RegFeeTier(dt.date(2025, 1, 1), 27.80, 0.000166, 8.30),
)

_FEE_STARTS = [t.start for t in REG_FEE_HISTORY]


def reg_fee_tier(day: dt.date) -> RegFeeTier:
    """Fee tier in force on ``day`` (clamped to the first tier before 1994)."""
    i = bisect_right(_FEE_STARTS, day) - 1
    return REG_FEE_HISTORY[max(i, 0)]


# --------------------------------------------------------------------------- #
# Tick-size eras -- used only for the *stress* scenario in which orders are NOT
# auction-filled and must cross a quoted spread.
# --------------------------------------------------------------------------- #

def era_tick_size(day: dt.date) -> float:
    """Minimum price increment in USD on the given date."""
    if day < dt.date(1997, 6, 24):
        return 0.125          # eighths
    if day < dt.date(2001, 4, 9):
        return 0.0625         # sixteenths
    return 0.01               # decimalisation


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CostModel:
    """Per-leg cost model in *cash* terms.

    All defaults describe **Alpaca-style commission-free auction execution at
    retail size**, which is the intended live configuration.
    """

    # Broker commission
    commission_per_share: float = 0.0
    commission_per_order: float = 0.0
    commission_min: float = 0.0
    commission_max_frac_notional: float | None = None

    # Regulatory (sell side only)
    regulatory_fees: bool = True

    # Execution quality
    slippage_bps_per_side: float = 0.0
    """Adverse price move vs. the reference print, per side.

    0.0 is correct for auction (MOC/OPG) fills, which transact *at* the print
    the backtest uses. Raise this to model non-auction execution.
    """

    cross_spread: bool = False
    """If True, add half the era tick size per side (stress scenario only)."""

    spread_ticks: float = 1.0
    """Assumed quoted spread in ticks when ``cross_spread`` is on."""

    # Auction impact: bps = coefficient * sqrt(participation_rate)
    impact_coefficient_bps: float = 0.0
    auction_volume_frac: float = 0.06
    """Share of daily volume assumed to print in each auction (open/close).

    ~6 % is a common order of magnitude for a liquid Nasdaq name; used only to
    size participation for the impact term. [verify-at-runtime with real
    auction-volume data if size ever becomes material.]
    """

    # Carry
    borrow_bps_annual: float = 0.0
    """Annualised stock-borrow rate, charged on short notional per calendar day."""

    margin_bps_annual: float = 0.0
    """Annualised margin rate, charged on borrowed notional per calendar day."""

    day_count: int = 360

    # Bookkeeping
    fractional_shares: bool = True
    label: str = "auction-retail"

    # ------------------------------------------------------------------ #

    def commission(self, shares: float, price: float) -> float:
        if shares <= 0:
            return 0.0
        c = self.commission_per_order + self.commission_per_share * shares
        c = max(c, self.commission_min)
        if self.commission_max_frac_notional is not None:
            c = min(c, self.commission_max_frac_notional * shares * price)
        return c

    def regulatory(self, shares: float, price: float, day: dt.date, side: Side) -> float:
        """SEC Section 31 + FINRA TAF. Both are charged on **sells** only."""
        if not self.regulatory_fees or side != "sell" or shares <= 0:
            return 0.0
        tier = reg_fee_tier(day)
        notional = shares * price
        sec = np.ceil(notional * tier.sec_per_million / 1e6 * 100.0) / 100.0
        taf = min(shares * tier.taf_per_share, tier.taf_cap) if tier.taf_per_share else 0.0
        taf = np.ceil(taf * 100.0) / 100.0
        return float(sec + taf)

    def regulatory_frac(
        self, price: float, day: dt.date, side: Side, *, notional: float | None = None
    ) -> float:
        """Regulatory fees as an **unrounded fraction of notional**.

        :meth:`regulatory` rounds up to whole cents, which is exactly right for
        the cash charged on a real order and exactly wrong as a rate. Deriving a
        per-notional rate from it by pricing a $1 order gives
        ``ceil(0.00278 cents) = $0.01`` -- a 100 bps "fee" on a 0.3 bps charge.
        Any vectorised path that works per unit of notional must use this method.

        The FINRA TAF cap is applied only when ``notional`` is supplied, since
        the cap binds on the order, not on the rate.
        """
        if not self.regulatory_fees or side != "sell" or price <= 0:
            return 0.0
        tier = reg_fee_tier(day)
        frac = tier.sec_per_million / 1e6
        if tier.taf_per_share:
            taf_frac = tier.taf_per_share / price
            if notional and notional > 0:
                taf_frac = min(taf_frac, tier.taf_cap / notional)
            frac += taf_frac
        return frac

    def commission_frac(self, price: float, *, notional: float | None = None) -> float:
        """Commission as a fraction of notional.

        Per-order and minimum components need a notional to be meaningful; with
        none supplied only the per-share component is returned, which is the
        correct limit for a large order.
        """
        if price <= 0:
            return 0.0
        frac = self.commission_per_share / price
        if notional and notional > 0:
            cash = self.commission_per_order + self.commission_per_share * (notional / price)
            cash = max(cash, self.commission_min)
            frac = cash / notional
        if self.commission_max_frac_notional is not None:
            frac = min(frac, self.commission_max_frac_notional)
        return frac

    def price_slippage_frac(self, day: dt.date, price: float) -> float:
        """Adverse price fraction per side, excluding size impact."""
        frac = self.slippage_bps_per_side * BPS
        if self.cross_spread and price > 0:
            frac += (era_tick_size(day) * self.spread_ticks / 2.0) / price
        return frac

    def impact_frac(self, shares: float, day_volume: float) -> float:
        """Square-root auction impact as a fraction of price."""
        if self.impact_coefficient_bps <= 0 or shares <= 0:
            return 0.0
        auction_vol = max(day_volume * self.auction_volume_frac, 1.0)
        if not np.isfinite(auction_vol):
            return 0.0
        participation = min(shares / auction_vol, 1.0)
        return self.impact_coefficient_bps * BPS * float(np.sqrt(participation))

    def fill_price(
        self, reference: float, side: Side, day: dt.date, *, shares: float = 0.0, day_volume: float = np.nan
    ) -> float:
        """Reference (auction print) price adjusted for slippage and impact."""
        adverse = self.price_slippage_frac(day, reference) + self.impact_frac(shares, day_volume)
        sign = 1.0 if side == "buy" else -1.0
        return reference * (1.0 + sign * adverse)

    def carry(self, notional: float, calendar_days: float, *, short: bool, leverage: float = 1.0) -> float:
        """Borrow and margin interest for a position held ``calendar_days``."""
        cost = 0.0
        if short and self.borrow_bps_annual:
            cost += abs(notional) * self.borrow_bps_annual * BPS * calendar_days / self.day_count
        borrowed = abs(notional) * max(leverage - 1.0, 0.0) / max(leverage, 1e-12)
        if self.margin_bps_annual and borrowed > 0:
            cost += borrowed * self.margin_bps_annual * BPS * calendar_days / self.day_count
        return cost

    def round_shares(self, shares: float) -> float:
        return float(shares) if self.fractional_shares else float(np.floor(shares))

    def with_(self, **kw) -> CostModel:
        return replace(self, **kw)


# --------------------------------------------------------------------------- #
# Named scenarios used by the report
# --------------------------------------------------------------------------- #

def scenario(name: str) -> CostModel:
    """Named cost scenarios, from best case to a deliberately brutal one."""
    scenarios: dict[str, CostModel] = {
        # Idealised: what the raw decomposition implies. Never a conclusion.
        "frictionless": CostModel(regulatory_fees=False, label="frictionless"),
        # Intended live setup: Alpaca paper/live, commission free, auction fills.
        "auction-retail": CostModel(label="auction-retail"),
        # Auction fills but the print is not perfectly attainable.
        "auction-1bp": CostModel(slippage_bps_per_side=0.5, label="auction-1bp"),
        # Same, with realistic small-size auction impact.
        "auction-impact": CostModel(
            slippage_bps_per_side=0.5, impact_coefficient_bps=10.0, label="auction-impact"
        ),
        # A traditional discount broker with per-share commission.
        "ibkr-like": CostModel(
            commission_per_share=0.0035, commission_min=0.35,
            commission_max_frac_notional=0.01, slippage_bps_per_side=0.5, label="ibkr-like",
        ),
        # Stress: the order misses the auction and must cross the quoted spread
        # at the tick size prevailing in that era. This is what kills the
        # pre-2001 portion of any such backtest.
        "cross-spread-era": CostModel(
            cross_spread=True, spread_ticks=1.0, slippage_bps_per_side=0.0, label="cross-spread-era",
        ),
        # Flat 5 bps per side, a common "be conservative" retail assumption.
        "pessimistic-5bp": CostModel(slippage_bps_per_side=5.0, label="pessimistic-5bp"),
    }
    if name not in scenarios:
        raise KeyError(f"unknown cost scenario {name!r}; have {sorted(scenarios)}")
    return scenarios[name]


ALL_SCENARIOS: tuple[str, ...] = (
    "frictionless",
    "auction-retail",
    "auction-1bp",
    "auction-impact",
    "ibkr-like",
    "pessimistic-5bp",
    "cross-spread-era",
)


# --------------------------------------------------------------------------- #
# Break-even analysis
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BreakEven:
    """Round-trip cost at which the edge exactly vanishes."""

    arithmetic_mean: float
    geometric_mean: float
    variance_drag: float
    breakeven_frac: float
    breakeven_bps: float
    n: int
    detail: dict[str, float] = field(default_factory=dict)


def breakeven_cost(returns: pd.Series | np.ndarray) -> BreakEven:
    """Maximum tolerable round-trip cost, computed geometrically.

    The strategy multiplies capital by ``(1+r_t)*(1-c)`` each session, so it
    breaks even when ``E[log(1+r)] + log(1-c) = 0``, i.e.

        c* = 1 - exp(-E[log(1+r)])

    Reporting the arithmetic mean instead overstates ``c*`` by roughly
    ``sigma^2/2`` -- which for MU overnight returns is on the same order as the
    edge itself.
    """
    r = np.asarray(returns, dtype="float64")
    r = r[np.isfinite(r)]
    if r.size == 0:
        return BreakEven(0.0, 0.0, 0.0, 0.0, 0.0, 0)
    gross = 1.0 + r
    valid = gross > 0
    logs = np.log(gross[valid])
    g = float(np.mean(logs))
    a = float(np.mean(r))
    c_star = float(-np.expm1(-g))
    return BreakEven(
        arithmetic_mean=a,
        geometric_mean=g,
        variance_drag=a - g,
        breakeven_frac=c_star,
        breakeven_bps=c_star / BPS,
        n=int(r.size),
        detail={
            "sigma": float(np.std(r, ddof=1)) if r.size > 1 else 0.0,
            "annualised_geometric": float(np.expm1(g * 252.0)),
            "wiped_periods": int((~valid).sum()),
        },
    )


def cost_drag_table(round_trip_bps: np.ndarray | list[float], trades_per_year: int = 252) -> pd.DataFrame:
    """Annualised drag of a per-round-trip cost. The table that ends arguments."""
    bps = np.asarray(round_trip_bps, dtype="float64")
    drag = np.expm1(trades_per_year * np.log1p(-bps * BPS))
    return pd.DataFrame(
        {"round_trip_bps": bps, "annual_drag": drag, "annual_drag_pct": drag * 100.0}
    ).set_index("round_trip_bps")
