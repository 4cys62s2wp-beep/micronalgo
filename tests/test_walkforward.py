"""Walk-forward: selection on train only, honest stitching, both verdicts."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from micronalgo.data.synthetic import from_returns, random_walk
from micronalgo.research import filters as F
from micronalgo.research.costs import scenario
from micronalgo.research.engine import BacktestConfig
from micronalgo.research.walkforward import Candidate, default_candidates, walk_forward


def _regime_bars(n: int = 4000, seed: int = 21) -> pd.DataFrame:
    """Overnight edge positive in calm regimes, negative in stormy ones.

    Calm sigma 1 %/session (~16 % annualised), stormy 3.5 % (~56 %): a
    30 %-annualised vol gate genuinely separates the two, with the 20-session
    lookback lagging each regime flip by a few weeks -- a realistic filter, not
    an oracle.
    """
    rng = np.random.default_rng(seed)
    calm = np.tile(np.repeat([True, False], 250), n // 500 + 1)[:n]
    sigma = np.where(calm, 0.010, 0.035)
    r_on = rng.normal(np.where(calm, 0.0012, -0.0012), sigma)
    r_id = rng.normal(-0.0002, sigma)
    return from_returns(r_on, r_id, start="2008-01-02")


def _separating_candidates() -> list[Candidate]:
    return [
        Candidate("baseline", lambda bars: None),
        Candidate("vol<30%", lambda bars: F.volatility_filter(bars, window=20, max_annual_vol=0.30)),
    ]


def test_a_genuinely_better_overlay_wins_out_of_sample():
    res = walk_forward(
        _regime_bars(),
        candidates=_separating_candidates(),
        config=BacktestConfig(cost=scenario("auction-retail")),
        train_sessions=756, test_sessions=252,
    )
    chosen = [f.chosen for f in res.folds]
    assert chosen.count("vol<30%") > len(chosen) / 2, chosen
    assert res.oos_total > res.baseline_total
    assert "minimum evidence" in res.verdict() or "beat the baseline out-of-sample" in res.verdict()


def test_useless_overlays_produce_a_deploy_nothing_verdict():
    """On a homogeneous random walk no overlay has an edge; whatever wins a
    training window is noise and the verdict must say so rather than celebrate."""
    bars = random_walk(3200, seed=7, start="2010-01-04", fat_tail_df=None)
    res = walk_forward(bars, config=BacktestConfig(cost=scenario("auction-retail")),
                       train_sessions=756, test_sessions=252)
    v = res.verdict()
    assert ("Deploy no filter" in v) or ("no overlay decision" in v), v


def test_test_windows_are_disjoint_and_never_overlap_training():
    res = walk_forward(_regime_bars(2600), candidates=_separating_candidates(),
                       train_sessions=756, test_sessions=252)
    seen: set = set()
    for f in res.folds:
        test_idx = res.oos_returns.loc[f.test_start:f.test_end].index
        assert f.train_end < f.test_start, "test must start strictly after training ends"
        overlap = seen.intersection(test_idx)
        assert not overlap, f"session used in two test windows: {sorted(overlap)[:3]}"
        seen.update(test_idx)
    assert len(res.oos_returns) == len(seen)
    assert res.oos_returns.index.equals(res.baseline_oos_returns.index)


def test_selection_uses_training_data_only():
    """Corrupting the test windows must not change which candidate is chosen."""
    bars = _regime_bars(2600)
    cands = _separating_candidates()
    a = walk_forward(bars, candidates=cands, train_sessions=756, test_sessions=252)

    # Wreck every test window's prices; training windows stay identical for
    # fold 0, whose choice therefore must not move.
    wrecked = bars.copy()
    f0 = a.folds[0]
    mask = wrecked.index >= f0.test_start
    for col in ("open", "close", "raw_open", "raw_close"):
        wrecked.loc[mask, col] = wrecked.loc[mask, col].to_numpy()[::-1]
    b = walk_forward(wrecked, candidates=cands, train_sessions=756, test_sessions=252)
    assert b.folds[0].chosen == a.folds[0].chosen


def test_baseline_candidate_is_mandatory():
    with pytest.raises(ValueError, match="baseline"):
        walk_forward(_regime_bars(2600), candidates=[Candidate("only", lambda b: None)])


def test_insufficient_history_raises():
    with pytest.raises(ValueError, match="at least"):
        walk_forward(random_walk(400, seed=1), train_sessions=756, test_sessions=252)


def test_default_menu_is_small_and_contains_the_baseline():
    names = [c.name for c in default_candidates()]
    assert "baseline" in names
    assert len(names) <= 6, "every candidate inflates the multiple-testing burden"


def test_tail_sessions_are_not_silently_dropped():
    """The newest data is what a deployment decision needs most; a tail shorter
    than a full test window must become a final short fold, not vanish."""
    bars = _regime_bars(756 + 252 + 100)  # one full fold plus a 100-session tail
    res = walk_forward(bars, candidates=_separating_candidates(),
                       train_sessions=756, test_sessions=252)
    dec_index = res.oos_returns.index
    assert dec_index[-1] == bars.index[-1], "the very last session must be in the OOS curve"
    assert len(res.folds) == 2
    assert (res.folds[-1].test_end - res.folds[-1].test_start).days < 300


def test_a_tiny_tail_is_ignored():
    bars = _regime_bars(756 + 252 + 5)
    res = walk_forward(bars, candidates=_separating_candidates(),
                       train_sessions=756, test_sessions=252)
    assert len(res.folds) == 1


def test_calmar_selection_prefers_holdability_over_raw_return():
    """When the problem is a drawdown you cannot sit through, selecting on
    return alone will happily pick the overlay that earns more and hurts more.
    Selecting on Calmar must not."""
    bars = _regime_bars()
    cfg = BacktestConfig(cost=scenario("auction-retail"))
    by_return = walk_forward(bars, config=cfg, score="geometric_mean")
    by_calmar = walk_forward(bars, config=cfg, score="calmar")

    dd_return, _ = by_return.oos_drawdown()
    dd_calmar, _ = by_calmar.oos_drawdown()
    assert dd_calmar >= dd_return, (
        f"calmar selection produced a deeper drawdown ({dd_calmar:.1%}) than "
        f"return selection ({dd_return:.1%})"
    )
    assert by_calmar.score == "calmar"
    assert "calmar" in by_calmar.verdict()


def test_oos_drawdown_reports_both_curves():
    res = walk_forward(_regime_bars(2600), candidates=_separating_candidates(),
                       train_sessions=756, test_sessions=252)
    chosen, baseline = res.oos_drawdown()
    assert chosen <= 0.0 and baseline <= 0.0
    assert "drawdown" in res.verdict()


def test_unknown_score_is_rejected():
    with pytest.raises(KeyError, match="unknown score"):
        walk_forward(_regime_bars(2600), score="sharpe_but_not_implemented")


def test_calmar_falls_back_when_a_window_never_draws_down():
    """Dividing by a zero drawdown would make an untested overlay look
    infinitely good and win every fold."""
    import numpy as np
    import pandas as pd

    from micronalgo.research.walkforward import _calmar, _geo_mean

    rising = pd.Series(np.full(200, 0.001))
    assert _calmar(rising) == pytest.approx(_geo_mean(rising))
    assert np.isfinite(_calmar(rising))
