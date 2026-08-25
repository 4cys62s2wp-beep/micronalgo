"""Costs, metrics, robustness, filters, and the study's reality check."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from micronalgo.data.schema import coerce
from micronalgo.data.synthetic import from_returns, random_walk
from micronalgo.research import filters as F
from micronalgo.research import metrics as M
from micronalgo.research import robustness as R
from micronalgo.research.costs import (
    breakeven_cost,
    cost_drag_table,
    era_tick_size,
    reg_fee_tier,
    scenario,
)
from micronalgo.research.returns import decompose
from micronalgo.research.study import Verdict, run_study, to_dict


# ---------------------------------------------------------------------- costs
def test_fee_tiers_are_effective_dated():
    assert reg_fee_tier(dt.date(2024, 6, 1)).sec_per_million == 27.80
    assert reg_fee_tier(dt.date(2021, 6, 1)).sec_per_million == 5.10
    assert reg_fee_tier(dt.date(1990, 1, 1)) is reg_fee_tier(dt.date(1994, 1, 1))


@pytest.mark.parametrize("day,tick", [
    (dt.date(1995, 1, 3), 0.125), (dt.date(1999, 1, 4), 0.0625), (dt.date(2005, 1, 3), 0.01),
])
def test_tick_size_eras(day, tick):
    assert era_tick_size(day) == tick


def test_breakeven_is_geometric_not_arithmetic():
    """The gap between the two is the variance drag, and it is not small."""
    rng = np.random.default_rng(7)
    r = rng.normal(0.0006, 0.02, 8000)
    be = breakeven_cost(r)
    assert be.geometric_mean < be.arithmetic_mean
    assert be.variance_drag == pytest.approx(be.arithmetic_mean - be.geometric_mean, rel=1e-12)
    # For small returns the drag is ~sigma^2/2.
    assert be.variance_drag == pytest.approx(0.02**2 / 2, rel=0.15)
    assert be.breakeven_frac == pytest.approx(-np.expm1(-be.geometric_mean), rel=1e-12)


def test_cost_drag_compounds_over_252_trades():
    tbl = cost_drag_table([5.0])
    assert tbl.loc[5.0, "annual_drag"] == pytest.approx((1 - 5e-4) ** 252 - 1, rel=1e-9)
    assert tbl.loc[5.0, "annual_drag"] < -0.10


def test_auction_execution_is_far_cheaper_than_crossing():
    day = dt.date(2025, 6, 10)
    auction = scenario("auction-retail")
    crossing = scenario("cross-spread-era")
    assert auction.price_slippage_frac(day, 100.0) == 0.0
    assert crossing.price_slippage_frac(day, 100.0) > 0.0


# -------------------------------------------------------------------- metrics
def test_max_drawdown_on_a_known_path():
    eq = pd.Series([100, 120, 60, 90, 150.0])
    mdd, days = M.max_drawdown(eq)
    assert mdd == pytest.approx(-0.5)
    assert days == 2


def test_deflated_sharpe_punishes_multiple_testing():
    r = decompose(random_walk(4000, seed=3)).frame["r_on"]
    assert M.deflated_sharpe(r, 1) > M.deflated_sharpe(r, 500)


def test_tail_concentration_detects_a_lottery():
    r = np.concatenate([np.zeros(990), np.full(10, 0.5)])
    assert M.tail_concentration(pd.Series(r), 10) == pytest.approx(1.0, rel=1e-9)


def test_metrics_survive_a_wiped_series():
    r = pd.Series([0.01, -1.0, 0.0, 0.0])
    m = M.compute(r)
    assert m.total_return == pytest.approx(-1.0)
    assert np.isfinite(m.max_drawdown)


# ----------------------------------------------------------------- robustness
def test_bootstrap_is_reproducible_and_brackets_the_observed():
    r = decompose(random_walk(3000, seed=9)).frame["r_on"]
    a = R.stationary_bootstrap(r, n_resamples=200, seed=42)
    b = R.stationary_bootstrap(r, n_resamples=200, seed=42)
    assert a.observed == b.observed and a.ci_low == b.ci_low
    assert a.ci_low <= a.observed <= a.ci_high
    assert a.block_length >= 1.0


def test_bootstrap_refuses_a_tiny_sample():
    with pytest.raises(ValueError, match="at least 30"):
        R.stationary_bootstrap(pd.Series(np.zeros(10)))


def test_drop_best_days_is_monotone():
    r = decompose(random_walk(2000, seed=4)).frame["r_on"]
    tbl = R.drop_best_days(r, ks=(0, 1, 10, 50))
    assert list(tbl["total_return"]) == sorted(tbl["total_return"], reverse=True)


def test_subperiods_cover_every_session():
    r = decompose(random_walk(3000, seed=6, start="1998-01-02")).frame["r_on"]
    for by in ("regime", "decade", "year"):
        assert int(R.subperiods(r, by=by)["n"].sum()) == len(r.dropna())


def _bounce_series(n: int, spread: float, seed: int = 4) -> pd.DataFrame:
    """A true random walk where every close prints on the bid and every open on
    the ask. Economically flat by construction; the overnight leg still shows a
    full spread of 'return'."""
    rng = np.random.default_rng(seed)
    v = np.cumprod(1 + rng.normal(0, 0.015, n)) * 100.0
    close_obs = v * (1 - spread / 2)
    open_obs = np.concatenate([[v[0] * (1 + spread / 2)], v[:-1] * (1 + spread / 2)])
    rng_range = np.abs(rng.normal(0, 0.012, n))
    hi = np.maximum(open_obs, close_obs) * (1 + rng_range) * (1 + spread / 2)
    lo = np.minimum(open_obs, close_obs) * (1 - rng_range) * (1 - spread / 2)
    df = pd.DataFrame({"open": open_obs, "close": close_obs, "high": hi, "low": lo},
                      index=pd.bdate_range("2000-01-03", periods=n))
    df.index.name = "date"
    return coerce(df)


@pytest.mark.parametrize("spread", [0.0125, 0.0050, 0.0010])
def test_pure_bid_ask_bounce_is_flagged(spread):
    """Zero economic edge must not be mistaken for one."""
    bars = _bounce_series(4000, spread)
    r_on = decompose(bars).frame["r_on"]
    chk = R.bid_ask_bounce_check(bars, r_on)
    # The artefact reproduces one full effective spread per session.
    assert chk["mean_overnight"] == pytest.approx(spread, rel=0.05)
    assert chk["within_spread"], chk


def test_genuine_edge_above_the_spread_is_not_flagged():
    rng = np.random.default_rng(8)
    bars = from_returns(rng.normal(0.0020, 0.008, 4000), rng.normal(-0.0005, 0.008, 4000),
                        start="2005-01-03", range_pad=0.0003)
    chk = R.bid_ask_bounce_check(bars, decompose(bars).frame["r_on"])
    assert not chk["within_spread"], chk
    assert chk["edge_over_spread"] > 2.0


def test_roll_estimator_is_blind_to_the_open_close_asymmetry():
    """Documents exactly why Corwin-Schultz is the primary estimator."""
    bars = _bounce_series(4000, 0.0125)
    roll = R.roll_spread(bars["close"])
    cs = R.corwin_schultz_spread(bars)
    # Every close prints on the same side, so close-to-close carries no bounce.
    assert not np.isfinite(roll["effective_spread"]) or roll["effective_spread"] < 0.5 * 0.0125
    assert cs["spread"] > 0.0125 * 0.5


# -------------------------------------------------------------------- filters
def test_every_filter_is_shifted(walk_bars):
    """An unshifted indicator would be a peek; assert the shift is real."""
    sma = walk_bars["close"].rolling(50, min_periods=50).mean()
    unshifted = (walk_bars["close"] > sma)
    shifted = F.trend_filter(walk_bars, window=50)
    aligned = unshifted.shift(1).fillna(False).astype(bool)
    assert shifted.equals(aligned.rename(shifted.name))
    assert not shifted.equals(unshifted.rename(shifted.name))


def test_extra_lag_is_stricter(walk_bars):
    a = F.trend_filter(walk_bars, window=50, extra_lag=0)
    b = F.trend_filter(walk_bars, window=50, extra_lag=1)
    assert b.equals(a.shift(1).fillna(False).astype(bool).rename(b.name))


def test_combine_treats_missing_as_do_not_trade(walk_bars):
    a = F.trend_filter(walk_bars, window=200)
    b = F.volatility_filter(walk_bars, window=20, max_annual_vol=0.5)
    both = F.combine(a, b)
    assert both.sum() <= min(a.sum(), b.sum())
    assert both.dtype == bool


def test_earnings_filter_blocks_the_arrival_session(walk_bars):
    entry = walk_bars.index[100].date()
    mask = F.earnings_filter(walk_bars, [entry], skip=True)
    # The trade is indexed under the session *after* the entry close.
    assert not bool(mask.iloc[101])
    assert bool(mask.iloc[100])
    assert (~mask).sum() == 1


def test_skip_earnings_without_a_calendar_says_so(settings, walk_bars):
    from micronalgo.live.strategy import should_trade_session

    s = settings.model_copy(update={"skip_earnings": True,
                                    "earnings_csv": settings.state_dir / "missing.csv"})
    allow, reason = should_trade_session(s, walk_bars, walk_bars.index[10].date())
    assert allow and "empty or missing" in reason


# ---------------------------------------------------------------------- study
def test_study_produces_a_verdict_and_serialises():
    bars = random_walk(2500, seed=1994, start="2012-01-03")
    result = run_study(bars, symbol="T", bootstrap_resamples=120)
    assert result.reality.verdict in (Verdict.PASS, Verdict.WARN, Verdict.FAIL)
    assert len(result.reality.criteria) >= 8
    payload = to_dict(result)
    import json
    json.loads(json.dumps(payload))  # must be JSON-clean, no NaN or numpy types
    assert payload["verdict"] == result.reality.verdict.value


def test_scenario_ordering_is_reflected_in_the_report_table():
    bars = random_walk(1200, seed=5, start="2015-01-02")
    r = run_study(bars, bootstrap_resamples=100)
    tbl = r.by_scenario
    assert tbl.loc["frictionless", "total_return"] >= tbl.loc["auction-retail", "total_return"]
    assert tbl.loc["auction-retail", "cost_per_trade_bps"] < 1.0


def test_metrics_handle_an_entirely_filtered_series():
    """A signal that filters out every session must not crash the metrics."""
    import pandas as pd

    for series in (pd.Series([], dtype="float64"), pd.Series([float("nan")] * 5)):
        m = M.compute(series)
        assert m.n == 0
        assert m.total_return == 0.0


def test_study_tolerates_a_scenario_list_without_the_primary():
    bars = random_walk(600, seed=2, start="2018-01-02")
    r = run_study(bars, cost_scenarios=("frictionless",), bootstrap_resamples=60)
    assert "auction-retail" in r.by_scenario.index


def test_empty_metrics_respect_exposure_fraction():
    import pandas as pd

    m = M.compute(pd.Series([], dtype="float64"), exposure_fraction=0.25)
    assert m.exposure_fraction == 0.25


def _flat_vol_series(n, spread, sigma, seed=3):
    """Random walk with a known spread and controllable volatility."""
    import numpy as np
    import pandas as pd

    from micronalgo.data.schema import coerce

    rng = np.random.default_rng(seed)
    v = np.cumprod(1 + rng.normal(0, sigma, n)) * 100.0
    rng_hl = np.abs(rng.normal(0, sigma, n))
    df = pd.DataFrame(
        {"open": v, "close": v,
         "high": v * (1 + rng_hl) * (1 + spread / 2),
         "low": v * (1 - rng_hl) * (1 - spread / 2)},
        index=pd.bdate_range("2000-01-03", periods=n),
    )
    df.index.name = "date"
    return coerce(df)


def test_spread_estimator_admits_when_it_cannot_measure():
    """Corwin-Schultz has a noise floor that scales with VOLATILITY, not spread.
    On a zero-spread series at MU-like volatility it reports ~74 bps. It must
    flag that as unreliable rather than hand back a number that looks like a
    measurement."""
    cs = R.corwin_schultz_spread(_flat_vol_series(6000, 0.0, 0.025))
    assert cs["spread"] > 0.004, "the noise floor is real; this test assumes it exists"
    assert not cs["reliable"], "a pure-noise estimate must not be presented as usable"
    assert cs["negative_share"] > R.NOISE_NEGATIVE_SHARE


def test_spread_estimator_is_trusted_when_the_spread_is_resolvable():
    cs = R.corwin_schultz_spread(_flat_vol_series(6000, 0.05, 0.025))
    assert cs["reliable"]
    assert cs["negative_share"] <= R.NOISE_NEGATIVE_SHARE


def test_unmeasurable_spread_does_not_manufacture_a_failure():
    """The bug this closes: a volatile stock got a FAIL verdict built entirely
    out of estimator noise, which discredits every other line in the report."""
    bars = _flat_vol_series(6000, 0.0, 0.025)
    r_on = decompose(bars).frame["r_on"]
    chk = R.bid_ask_bounce_check(bars, r_on)
    assert not chk["determinable"]
    assert not chk["within_spread"], "noise must not be reported as 'edge inside the spread'"

    result = run_study(bars, bootstrap_resamples=80)
    line = next(c for c in result.reality.criteria if c.name == "edge vs. effective spread")
    assert line.verdict is Verdict.WARN
    assert "not measurable" in line.value
    # It must stay a live concern, not become an all-clear.
    assert "concern is real" in line.explanation
