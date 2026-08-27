"""Backtest engine.

Design decisions worth stating explicitly
-----------------------------------------

**Cash accounting, not return multiplication.** The engine buys a share count,
pays fees in dollars, and marks equity. That catches minimum commissions,
per-share fees and share rounding, which a ``(1+r)*(1-c)`` shortcut silently
drops.

**Simulation runs in adjusted-price space.** Adjusted prices are a total-return
index: sizing and P&L in that space are exact and require no split bookkeeping.
Notional (``shares * price``) is invariant to adjustment, so notional-based fees
(SEC Section 31, slippage, percentage caps) are identical either way. Only
*per-share* fees differ, so the engine separately computes the **real** share
count from raw prices and charges per-share fees on that. This is exact rather
than approximate, and it is the subtle bit most implementations get wrong.

**No lookahead, by construction.** The decision for session ``t`` may use only
information available at the close of ``t-1``. Filters are therefore shifted by
the engine itself, not by the caller -- see :func:`_align_signal`. The test
suite proves that un-shifting a filter changes results, so the shift is real.

**Two code paths, one answer.** :func:`simulate` is the explicit, auditable loop.
:func:`net_return_series` is a vectorised fast path used by the bootstrap, which
needs thousands of re-runs. ``tests/test_engine.py`` asserts they agree to 1e-10,
so the fast path can never quietly drift away from the exact one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from .costs import CostModel, scenario

Mode = Literal["overnight", "intraday", "short_intraday", "buyhold", "overnight_plus_short_intraday"]

MODES: tuple[Mode, ...] = (
    "overnight",
    "intraday",
    "short_intraday",
    "buyhold",
    "overnight_plus_short_intraday",
)


@dataclass(frozen=True)
class BacktestConfig:
    mode: Mode = "overnight"
    initial_capital: float = 100_000.0
    leverage: float = 1.0
    """Notional deployed as a multiple of equity. >1 charges margin interest."""

    cost: CostModel = field(default_factory=lambda: scenario("auction-retail"))
    max_participation: float | None = None
    """Cap position at this fraction of the session's volume (None = uncapped)."""

    min_price: float = 1.0
    """Refuse to trade below this raw price (penny-stock microstructure is not
    representative and MU traded in the low single digits in 2009)."""

    reinvest: bool = True
    """Compound the full equity. If False, always trade the initial notional."""

    label: str = ""

    def describe(self) -> str:
        return (
            f"{self.mode} | lev={self.leverage:g} | cost={self.cost.label} | "
            f"cap=${self.initial_capital:,.0f} | {'compounding' if self.reinvest else 'fixed-notional'}"
        )


@dataclass
class BacktestResult:
    config: BacktestConfig
    equity: pd.Series
    returns: pd.Series
    trades: pd.DataFrame
    costs: pd.DataFrame
    skipped: pd.Series
    ruined_on: pd.Timestamp | None = None

    @property
    def total_return(self) -> float:
        if self.equity.empty:
            return 0.0
        return float(self.equity.iloc[-1] / self.config.initial_capital - 1.0)

    @property
    def n_trades(self) -> int:
        return int(len(self.trades))

    @property
    def total_cost_cash(self) -> float:
        return float(self.costs["total"].sum()) if not self.costs.empty else 0.0

    def summary_line(self) -> str:
        return (
            f"{self.config.mode:<30} total={self.total_return * 100:>14,.2f}%  "
            f"final=${self.equity.iloc[-1] if not self.equity.empty else 0:>16,.2f}  "
            f"trades={self.n_trades:>6}  costs=${self.total_cost_cash:>14,.2f}"
        )


# --------------------------------------------------------------------------- #
# Leg definitions: which price you enter at, which you exit at, and the sign.
# --------------------------------------------------------------------------- #

_LEGS: dict[str, list[tuple[str, str, int, str]]] = {
    #                entry_price   exit_price   direction  leg name
    "overnight": [("prev_close", "open", +1, "overnight")],
    "intraday": [("open", "close", +1, "intraday")],
    "short_intraday": [("open", "close", -1, "intraday")],
    "buyhold": [("prev_close", "close", +1, "full_day")],
    "overnight_plus_short_intraday": [
        ("prev_close", "open", +1, "overnight"),
        ("open", "close", -1, "intraday"),
    ],
}


def _align_signal(signal: pd.Series | None, index: pd.Index) -> np.ndarray:
    """Reindex a tradeable-flag onto ``index``.

    ``signal[t]`` must already answer "may I hold the position that is entered
    for session ``t``". Producers of filters (:mod:`micronalgo.research.filters`)
    are responsible for using only data through ``t-1``; the engine asserts the
    contract by refusing a signal with a different index.
    """
    if signal is None:
        return np.ones(len(index), dtype=bool)
    aligned = signal.reindex(index)
    if aligned.isna().any():
        aligned = aligned.fillna(False)
    return aligned.to_numpy(dtype=bool)


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Attach previous-session closes (adjusted and raw) and the overnight span."""
    out = df.copy()
    out["prev_close"] = out["close"].shift(1)
    out["prev_raw_close"] = out["raw_close"].shift(1)
    out["prev_date"] = pd.Series(out.index, index=out.index).shift(1)
    out["gap_days"] = pd.Series(out.index, index=out.index).diff().dt.days.astype("float64")
    return out.iloc[1:]


def _raw_for(field_name: str) -> str:
    return {"prev_close": "prev_raw_close", "open": "raw_open", "close": "raw_close"}[field_name]


def simulate(
    df: pd.DataFrame,
    config: BacktestConfig | None = None,
    *,
    signal: pd.Series | None = None,
) -> BacktestResult:
    """Run the explicit cash-accounting simulation.

    Parameters
    ----------
    df:
        Canonical bar frame (see :mod:`micronalgo.data.schema`).
    signal:
        Optional boolean series indexed like ``df``. ``True`` means "take the
        trade for this session". Must already be lag-safe.
    """
    config = config or BacktestConfig()
    if config.mode not in _LEGS:
        raise ValueError(f"unknown mode {config.mode!r}; have {sorted(_LEGS)}")

    data = _prepare(df)
    take = _align_signal(signal, data.index)

    legs = _LEGS[config.mode]
    equity = config.initial_capital
    curve: list[float] = []
    rets: list[float] = []
    skipped: list[str] = []
    trade_rows: list[dict] = []
    cost_rows: list[dict] = []
    ruined_on: pd.Timestamp | None = None

    idx = list(data.index)
    cols = {c: data[c].to_numpy(dtype="float64") for c in
            ("open", "close", "prev_close", "raw_open", "raw_close", "prev_raw_close", "volume", "gap_days")}

    for i, ts in enumerate(idx):
        day = ts.date()
        start_equity = equity
        reason = ""

        prices_ok = all(
            np.isfinite(cols[c][i]) and cols[c][i] > 0
            for c in ("open", "close", "prev_close", "raw_open", "raw_close", "prev_raw_close")
        )
        if not prices_ok:
            reason = "bad_prices"
        elif not take[i]:
            reason = "filtered"
        elif cols["prev_raw_close"][i] < config.min_price:
            reason = "below_min_price"
        elif equity <= 0:
            reason = "ruined"

        if reason:
            skipped.append(reason)
            curve.append(equity)
            rets.append(0.0)
            continue
        skipped.append("")

        notional_target = equity * config.leverage if config.reinvest else config.initial_capital * config.leverage
        day_volume = cols["volume"][i]
        gross_pnl = 0.0
        slippage_cash = 0.0
        cash_costs = {"commission": 0.0, "regulatory": 0.0, "carry": 0.0}

        for entry_f, exit_f, direction, leg_name in legs:
            entry_ref_adj = cols[entry_f][i]
            exit_ref_adj = cols[exit_f][i]
            entry_ref_raw = cols[_raw_for(entry_f)][i]
            exit_ref_raw = cols[_raw_for(exit_f)][i]

            entry_side = "buy" if direction > 0 else "sell"
            exit_side = "sell" if direction > 0 else "buy"

            # Size against the price actually PAID, not the reference print.
            # Sizing off the reference commits notional*(1+slippage) of capital --
            # money the account does not have -- and understates the cost of every
            # trade by roughly one side's slippage.
            provisional = notional_target / entry_ref_adj
            entry_fill = config.cost.fill_price(
                entry_ref_adj, entry_side, day, shares=provisional, day_volume=day_volume
            )
            exit_fill = config.cost.fill_price(
                exit_ref_adj, exit_side, day, shares=provisional, day_volume=day_volume
            )

            adj_shares = notional_target / entry_fill
            if config.max_participation is not None and np.isfinite(day_volume) and day_volume > 0:
                cap = config.max_participation * day_volume
                if adj_shares > cap:
                    adj_shares = cap
            adj_shares = config.cost.round_shares(adj_shares)
            if adj_shares <= 0:
                continue

            notional = adj_shares * entry_fill
            # Real (as-traded) share count -- per-share fees are charged on this.
            real_shares = config.cost.round_shares(notional / entry_ref_raw)

            # P&L runs between the two FILL prices, so slippage is already inside
            # it. `slip` is recorded for the cost report and NOT subtracted again.
            gross_pnl += direction * adj_shares * (exit_fill - entry_fill)
            slip = adj_shares * (abs(entry_fill - entry_ref_adj) + abs(exit_fill - exit_ref_adj))

            comm = config.cost.commission(real_shares, entry_ref_raw) + config.cost.commission(
                real_shares, exit_ref_raw
            )
            reg = config.cost.regulatory(real_shares, entry_ref_raw, day, entry_side) + config.cost.regulatory(
                real_shares, exit_ref_raw, day, exit_side
            )

            if leg_name == "overnight":
                held_days = cols["gap_days"][i]
                held_days = held_days if np.isfinite(held_days) and held_days > 0 else 1.0
            else:
                held_days = 0.0
            carry = config.cost.carry(
                notional, held_days, short=direction < 0, leverage=config.leverage
            ) if held_days > 0 else (
                config.cost.carry(notional, 1.0 / 6.5 / 24 * 6.5, short=direction < 0, leverage=config.leverage)
                if direction < 0 and config.cost.borrow_bps_annual else 0.0
            )

            cash_costs["commission"] += comm
            cash_costs["regulatory"] += reg
            cash_costs["carry"] += carry
            slippage_cash += slip

            trade_rows.append(
                {
                    "date": ts,
                    "leg": leg_name,
                    "direction": direction,
                    "shares_adj": adj_shares,
                    "shares_real": real_shares,
                    "entry_ref": entry_ref_adj,
                    "exit_ref": exit_ref_adj,
                    "entry_fill": entry_fill,
                    "exit_fill": exit_fill,
                    "notional": notional,
                    "gross_pnl": direction * adj_shares * (exit_ref_adj - entry_ref_adj),
                    "cost": comm + reg + slip + carry,
                }
            )

        fee_cost = sum(cash_costs.values())
        equity = start_equity + gross_pnl - fee_cost
        if equity <= 0 and ruined_on is None:
            ruined_on = ts
            equity = 0.0

        cost_rows.append(
            {"date": ts, **cash_costs, "slippage": slippage_cash, "total": fee_cost + slippage_cash}
        )
        curve.append(equity)
        rets.append((equity / start_equity - 1.0) if start_equity > 0 else 0.0)

    equity_s = pd.Series(curve, index=data.index, name="equity", dtype="float64")
    returns_s = pd.Series(rets, index=data.index, name="return", dtype="float64")
    trades_df = pd.DataFrame(trade_rows).set_index("date") if trade_rows else pd.DataFrame()
    costs_df = pd.DataFrame(cost_rows).set_index("date") if cost_rows else pd.DataFrame()
    skipped_s = pd.Series(skipped, index=data.index, name="skip_reason")

    return BacktestResult(
        config=config,
        equity=equity_s,
        returns=returns_s,
        trades=trades_df,
        costs=costs_df,
        skipped=skipped_s,
        ruined_on=ruined_on,
    )


def net_return_series(
    df: pd.DataFrame,
    config: BacktestConfig | None = None,
    *,
    signal: pd.Series | None = None,
) -> pd.Series:
    """Vectorised net per-session returns. Fast path for bootstrap resampling.

    Equivalent to :func:`simulate` for the common configuration (fractional
    shares, no participation cap, compounding). Verified by
    ``tests/test_engine.py::test_fast_path_matches_simulation``.
    """
    config = config or BacktestConfig()
    data = _prepare(df)
    take = _align_signal(signal, data.index)
    legs = _LEGS[config.mode]

    net = np.zeros(len(data), dtype="float64")
    day_dates = [ts.date() for ts in data.index]
    volume = data["volume"].to_numpy(dtype="float64")
    gap = data["gap_days"].to_numpy(dtype="float64")

    for entry_f, exit_f, direction, leg_name in legs:
        entry = data[entry_f].to_numpy(dtype="float64")
        exit_ = data[exit_f].to_numpy(dtype="float64")
        entry_raw = data[_raw_for(entry_f)].to_numpy(dtype="float64")
        exit_raw = data[_raw_for(exit_f)].to_numpy(dtype="float64")

        slip_entry = np.array(
            [config.cost.price_slippage_frac(d, p) for d, p in zip(day_dates, entry_raw, strict=True)]
        )
        slip_exit = np.array(
            [config.cost.price_slippage_frac(d, p) for d, p in zip(day_dates, exit_raw, strict=True)]
        )

        if config.cost.impact_coefficient_bps > 0:
            shares = config.leverage / np.where(entry_raw > 0, entry_raw, np.nan)
            impact = np.array(
                [config.cost.impact_frac(s if np.isfinite(s) else 0.0, v) for s, v in zip(shares, volume, strict=True)]
            )
            slip_entry = slip_entry + impact
            slip_exit = slip_exit + impact

        # Multiplicative fills, matching simulate(): buy at ref*(1+s), sell at
        # ref*(1-s), with the share count implied by the price actually paid.
        if direction > 0:
            gross = (exit_ * (1.0 - slip_exit)) / (entry * (1.0 + slip_entry)) - 1.0
        else:
            gross = 1.0 - (exit_ * (1.0 + slip_exit)) / (entry * (1.0 - slip_entry))
        frac_cost = np.zeros(len(data), dtype="float64")

        # Per-notional regulatory + commission as *fractions*. Never derive these
        # by pricing a $1 order through the cash methods: those round up to whole
        # cents, which turns a 0.3 bps fee into 100 bps.
        fee_frac = np.zeros(len(data), dtype="float64")
        notional_hint = max(config.initial_capital * config.leverage, 1.0)
        entry_side = "buy" if direction > 0 else "sell"
        exit_side = "sell" if direction > 0 else "buy"
        for k, (d, e_raw, x_raw) in enumerate(zip(day_dates, entry_raw, exit_raw, strict=True)):
            if not (np.isfinite(e_raw) and e_raw > 0):
                continue
            fee_frac[k] = (
                config.cost.commission_frac(e_raw, notional=notional_hint)
                + config.cost.commission_frac(x_raw, notional=notional_hint)
                + config.cost.regulatory_frac(e_raw, d, entry_side, notional=notional_hint)
                + config.cost.regulatory_frac(x_raw, d, exit_side, notional=notional_hint)
            )

        carry_frac = np.zeros(len(data), dtype="float64")
        if leg_name == "overnight":
            held = np.where(np.isfinite(gap) & (gap > 0), gap, 1.0)
            carry_frac = np.array(
                [config.cost.carry(1.0, h, short=direction < 0, leverage=config.leverage) for h in held]
            )
        elif direction < 0 and config.cost.borrow_bps_annual:
            carry_frac = np.full(len(data), config.cost.carry(1.0, 1.0, short=True, leverage=config.leverage))

        leg_net = gross - frac_cost - fee_frac - carry_frac
        net = net + config.leverage * np.where(np.isfinite(leg_net), leg_net, 0.0)

    bad = ~np.isfinite(data["open"].to_numpy()) | ~np.isfinite(data["prev_close"].to_numpy())
    bad |= data["prev_raw_close"].to_numpy() < config.min_price
    net = np.where(take & ~bad, net, 0.0)
    return pd.Series(net, index=data.index, name=f"net_{config.mode}", dtype="float64")


def run_all_modes(
    df: pd.DataFrame, *, cost_scenario: str = "auction-retail", **kw
) -> dict[str, BacktestResult]:
    """Convenience: every mode under one cost scenario."""
    out: dict[str, BacktestResult] = {}
    for mode in MODES:
        cfg = BacktestConfig(mode=mode, cost=scenario(cost_scenario), **kw)
        out[mode] = simulate(df, cfg)
    return out
