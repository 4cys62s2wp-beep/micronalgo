"""The decomposition arithmetic, against closed-form answers."""

from __future__ import annotations

import numpy as np
import pytest

from micronalgo.data.synthetic import constant_series, from_returns, random_walk
from micronalgo.research.returns import compound, decompose, equity_curve, identity_error


def test_identity_holds_to_machine_precision(walk_bars):
    d = decompose(walk_bars)
    assert d.max_identity_error < 1e-12


def test_constant_series_matches_closed_form():
    n, r_on, r_id = 500, 0.0010, -0.0005
    d = decompose(constant_series(n, r_on=r_on, r_id=r_id))
    assert compound(d.frame["r_on"]) == pytest.approx((1 + r_on) ** n - 1, rel=1e-12)
    assert compound(d.frame["r_id"]) == pytest.approx((1 + r_id) ** n - 1, rel=1e-12)
    assert compound(d.frame["r_cc"]) == pytest.approx(
        ((1 + r_on) * (1 + r_id)) ** n - 1, rel=1e-12
    )


def test_compounded_totals_also_multiply_out(walk_bars):
    """The identity survives compounding, not just the per-session level."""
    d = decompose(walk_bars).frame
    lhs = (1 + compound(d["r_on"])) * (1 + compound(d["r_id"]))
    rhs = 1 + compound(d["r_cc"])
    assert lhs == pytest.approx(rhs, rel=1e-10)


def test_first_session_is_dropped(walk_bars):
    """It has no previous close, so its overnight return is undefined.

    Keeping it as 0.0 would hand the strategy a free session.
    """
    d = decompose(walk_bars)
    assert len(d.frame) == len(walk_bars) - 1
    assert d.frame.index[0] == walk_bars.index[1]


def test_summing_is_not_compounding():
    """Guards the single most common error in write-ups of this anomaly."""
    d = decompose(random_walk(2000, seed=5)).frame
    assert abs(float(d["r_on"].sum()) - compound(d["r_on"])) > 0.05


def test_total_loss_is_absorbing():
    r_on = np.array([0.01, -1.0, 0.5, 0.5])
    r_id = np.zeros(4)
    d = decompose(from_returns(r_on, r_id)).frame
    curve = equity_curve(d["r_on"])
    assert curve.iloc[-1] == pytest.approx(0.0, abs=1e-12)
    assert compound(d["r_on"]) == pytest.approx(-1.0)


def test_compound_handles_long_series_without_overflow():
    r = np.full(50_000, 0.001)
    assert np.isfinite(compound(r))


def test_identity_error_flags_mismatched_sources(walk_bars):
    """It is a wiring check: it fires only when r_cc disagrees with the prices."""
    d = decompose(walk_bars).frame.copy()
    assert identity_error(d) < 1e-12
    d.loc[d.index[10], "r_cc"] = 0.5
    assert identity_error(d) > 0.1


def test_unsorted_input_is_rejected(walk_bars):
    with pytest.raises(ValueError, match="sorted"):
        decompose(walk_bars.iloc[::-1])
