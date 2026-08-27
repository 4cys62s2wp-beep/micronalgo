"""Backtest engine: closed-form agreement, cost accounting, no lookahead."""

from __future__ import annotations

import numpy as np
import pytest

from micronalgo.data.synthetic import constant_series, random_walk
from micronalgo.research.costs import CostModel, scenario
from micronalgo.research.engine import BacktestConfig, net_return_series, simulate
from micronalgo.research.returns import compound, decompose


def test_frictionless_matches_closed_form():
    n, r_on = 500, 0.0010
    bars = constant_series(n, r_on=r_on, r_id=-0.0005)
    res = simulate(bars, BacktestConfig(mode="overnight", cost=scenario("frictionless"),
                                        initial_capital=1.0))
    assert res.total_return == pytest.approx((1 + r_on) ** n - 1, rel=1e-9)


@pytest.mark.parametrize("mode", ["overnight", "intraday", "buyhold"])
def test_modes_match_the_decomposition(mode, walk_bars):
    col = {"overnight": "r_on", "intraday": "r_id", "buyhold": "r_cc"}[mode]
    expected = compound(decompose(walk_bars).frame[col])
    res = simulate(walk_bars, BacktestConfig(mode=mode, cost=scenario("frictionless")))
    assert res.total_return == pytest.approx(expected, rel=1e-9)


def test_fast_path_matches_simulation(walk_bars):
    """The bootstrap uses the vectorised path; it must never drift from the exact one.

    The two are not bit-identical when regulatory fees are on, and correctly so:
    `simulate` rounds the cash fee up to whole cents on each real order, while the
    vectorised path works in unrounded fractions of notional. On a $100k account
    that gap is under a basis point over the whole history.
    """
    # Only `frictionless` disables regulatory fees, so only it is exact; the
    # others carry the cent-rounding gap quantified in the next test.
    for name, tol in (("frictionless", 1e-10), ("auction-retail", 2e-3), ("pessimistic-5bp", 2e-3)):
        cfg = BacktestConfig(mode="overnight", cost=scenario(name), initial_capital=100_000.0)
        slow = simulate(walk_bars, cfg).total_return
        fast = compound(net_return_series(walk_bars, cfg))
        assert fast == pytest.approx(slow, rel=tol), f"paths disagree for {name}"


def test_fast_path_gap_is_only_cent_rounding(walk_bars):
    """Prove the residual gap is rounding, not a modelling difference.

    Cent rounding is a fixed cash amount per order, so its relative effect must
    shrink as the account grows. If the gap were a real disagreement it would
    stay put.
    """
    gaps = []
    for capital in (10_000.0, 100_000.0, 1_000_000.0):
        cfg = BacktestConfig(mode="overnight", cost=scenario("auction-retail"),
                             initial_capital=capital)
        slow = simulate(walk_bars, cfg).total_return
        fast = compound(net_return_series(walk_bars, cfg))
        gaps.append(abs(fast - slow) / abs(slow))
    assert gaps == sorted(gaps, reverse=True), gaps
    assert gaps[-1] < 1e-4


def test_fee_fraction_is_not_cent_rounded():
    """A per-notional rate must never be derived by pricing a $1 order.

    `regulatory()` rounds up to whole cents -- right for an order's cash, and
    catastrophic as a rate: a $1 notional yields a $0.01 SEC fee, i.e. 100 bps
    where the true rate is 0.278 bps.
    """
    import datetime as dt

    m = scenario("auction-retail")
    day = dt.date(2025, 6, 10)
    frac = m.regulatory_frac(100.0, day, "sell")
    assert 0.2e-4 < frac < 0.4e-4, f"{frac * 1e4:.4f} bps"

    naive = m.regulatory(1.0 / 100.0, 100.0, day, "sell")  # the trap: a $1 notional
    assert naive / 1.0 > 100 * frac, "the cent-rounding trap is no longer reproducible"

    # On a realistic order the cash and fractional views agree closely.
    cash_bps = m.regulatory(250.0, 100.0, day, "sell") / 25_000.0 * 1e4
    assert cash_bps == pytest.approx(frac * 1e4, abs=0.02)


def test_short_intraday_is_the_mirror_of_intraday(walk_bars):
    long_r = net_return_series(walk_bars, BacktestConfig(mode="intraday", cost=scenario("frictionless")))
    short_r = net_return_series(walk_bars, BacktestConfig(mode="short_intraday", cost=scenario("frictionless")))
    assert np.allclose(long_r.to_numpy(), -short_r.to_numpy(), atol=1e-12)


def test_cost_is_charged_once_per_leg():
    """A flat per-side slippage must show up as exactly 2x per round trip."""
    n = 200
    bars = constant_series(n, r_on=0.0, r_id=0.0)
    bps = 10.0
    cfg = BacktestConfig(mode="overnight", cost=CostModel(slippage_bps_per_side=bps, regulatory_fees=False),
                         initial_capital=1.0)
    res = simulate(bars, cfg)
    # entry at price*(1+s), exit at price*(1-s) -> multiplier (1-s)/(1+s) per trade
    s = bps * 1e-4
    expected = ((1 - s) / (1 + s)) ** n - 1
    assert res.total_return == pytest.approx(expected, rel=1e-6)


def test_regulatory_fees_apply_to_sells_only():
    m = scenario("auction-retail")
    import datetime as dt
    day = dt.date(2025, 6, 10)
    assert m.regulatory(1000, 100.0, day, "buy") == 0.0
    assert m.regulatory(1000, 100.0, day, "sell") > 0.0


def test_costs_reduce_return_monotonically(walk_bars):
    names = ["frictionless", "auction-retail", "auction-1bp", "pessimistic-5bp"]
    totals = [
        simulate(walk_bars, BacktestConfig(mode="overnight", cost=scenario(n))).total_return
        for n in names
    ]
    assert totals == sorted(totals, reverse=True), dict(zip(names, totals, strict=True))


def test_no_lookahead_signal_shift_changes_the_result(walk_bars):
    """A signal is only a signal if using tomorrow's version changes the answer.

    If shifting made no difference the engine would be ignoring the signal.
    """
    from micronalgo.research import filters as F

    cfg = BacktestConfig(mode="overnight", cost=scenario("frictionless"))
    honest = F.trend_filter(walk_bars, window=50)
    peeking = honest.shift(-1).fillna(False).astype(bool)
    peeking.name = "peek"

    a = simulate(walk_bars, cfg, signal=honest).total_return
    b = simulate(walk_bars, cfg, signal=peeking).total_return
    assert a != pytest.approx(b, rel=1e-6)


def test_filtered_sessions_are_flat_not_negative(walk_bars):
    from micronalgo.research import filters as F

    sig = F.weekday_filter(walk_bars, weekdays=(0,))
    res = simulate(walk_bars, BacktestConfig(mode="overnight", cost=scenario("auction-retail")), signal=sig)
    skipped = res.returns[res.skipped == "filtered"]
    assert (skipped == 0.0).all()
    assert res.n_trades == int(sig.reindex(res.returns.index).sum())


def test_participation_cap_limits_size():
    bars = constant_series(50, r_on=0.001, r_id=0.0, initial_close=100.0)
    bars = bars.assign(volume=1000.0)
    capped = simulate(bars, BacktestConfig(mode="overnight", cost=scenario("frictionless"),
                                           initial_capital=1_000_000.0, max_participation=0.001))
    assert (capped.trades["shares_adj"] <= 1.0 + 1e-9).all()


def test_bad_prices_are_skipped_not_crashed(walk_bars):
    bars = walk_bars.copy()
    bars.loc[bars.index[100], "open"] = np.nan
    bars.loc[bars.index[200], "close"] = 0.0
    res = simulate(bars, BacktestConfig(mode="overnight", cost=scenario("frictionless")))
    assert (res.skipped == "bad_prices").sum() >= 1
    assert np.isfinite(res.total_return)


def test_ruin_is_absorbing():
    """5x leverage into a -25 % overnight gap wipes the account, and it stays wiped.

    This is not a hypothetical for this strategy: you hold every earnings gap
    overnight with no possibility of a stop.
    """
    bars = constant_series(10, r_on=-0.25, r_id=0.0)
    res = simulate(bars, BacktestConfig(mode="overnight", cost=scenario("frictionless"),
                                        initial_capital=100_000.0, leverage=5.0))
    assert res.ruined_on is not None
    assert float(res.equity.iloc[-1]) == pytest.approx(0.0, abs=1e-9)
    assert (res.equity >= 0).all()


def test_per_share_fees_use_raw_prices_not_adjusted():
    """A per-share fee on a heavily back-adjusted history must not be scaled.

    With adj_factor 0.1 the adjusted price is 10x smaller, so sizing in adjusted
    space implies 10x the shares. Charging a per-share fee on that count would
    overstate costs tenfold.
    """
    bars = constant_series(50, r_on=0.0, r_id=0.0, initial_close=10.0)
    scaled = bars.copy()
    for c in ("raw_open", "raw_high", "raw_low", "raw_close"):
        scaled[c] = scaled[c] * 10.0
    scaled["adj_factor"] = 0.1

    model = CostModel(commission_per_share=0.01, regulatory_fees=False)
    # reinvest=False keeps the notional constant so the comparison is not
    # distorted by the two runs compounding at different rates.
    plain = simulate(bars, BacktestConfig(mode="overnight", cost=model,
                                          initial_capital=10_000.0, reinvest=False))
    adj = simulate(scaled, BacktestConfig(mode="overnight", cost=model,
                                          initial_capital=10_000.0, reinvest=False))
    # The as-traded price is 10x higher, so 1/10th the shares and 1/10th the fees.
    assert adj.total_cost_cash == pytest.approx(plain.total_cost_cash / 10.0, rel=1e-6)


def test_golden_regression_pins_the_numbers():
    """Freeze a known configuration so a refactor cannot silently move results."""
    bars = random_walk(2000, seed=777, start="2010-01-04")
    res = simulate(bars, BacktestConfig(mode="overnight", cost=scenario("auction-retail"),
                                        initial_capital=100_000.0))
    assert res.n_trades == 2000
    assert res.total_return == pytest.approx(1.1571636243530938, rel=1e-9)
    assert float(res.equity.iloc[-1]) == pytest.approx(215716.3624353094, rel=1e-9)
