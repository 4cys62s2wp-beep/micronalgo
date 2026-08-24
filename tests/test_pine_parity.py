"""The Pine sources: offline lint, and parity with the Python engine.

TradingView cannot be reached from this test suite, so the Pine scripts get the
two checks that are possible without a compiler:

1. **Mechanical lint** (:mod:`scripts.pine_lint`) -- version tag, bracket
   balance, string termination, and Pine's indentation rule, where a block is a
   multiple of four spaces and a line continuation must *not* be.

2. **Math parity.** The indicator's bar loop is transcribed literally into
   Python here, including Pine's ``int / int -> int`` semantics, and its output
   is compared with the verified engine. This is what caught the hit-rate bug:
   ``nWinOn / nBars`` is integer division in Pine and displayed 0 % on every
   chart.

The transcription is a model of the source, so the test also asserts that the
specific expressions it models are still present. Change the Pine and this test
fails until the transcription is updated with it.
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from micronalgo.data.synthetic import random_walk
from micronalgo.research import metrics as M
from micronalgo.research.costs import breakeven_cost
from micronalgo.research.returns import compound, decompose

ROOT = Path(__file__).resolve().parents[1]
PINE_DIR = ROOT / "pine"
INDICATOR = PINE_DIR / "overnight_vs_intraday.pine"
STRATEGY = PINE_DIR / "micron_overnight_strategy.pine"


# --------------------------------------------------------------------- lint
@pytest.mark.parametrize("path", [INDICATOR, STRATEGY], ids=lambda p: p.name)
def test_pine_lint(path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "pine_lint.py"), str(path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("path", [INDICATOR, STRATEGY], ids=lambda p: p.name)
def test_chart_setting_guards_are_present(path):
    """Extended hours and synthetic chart types corrupt open/close silently, so
    both scripts must stop rather than warn."""
    src = path.read_text()
    assert "syminfo.session != session.regular" in src
    assert "chart.is_standard" in src
    assert src.count("runtime.error(") >= 2


def test_strategy_refuses_a_daily_chart():
    """On daily bars Pine cannot fill one leg at the close and the other at the
    next open, so it would quietly test a different strategy."""
    src = STRATEGY.read_text()
    assert "not timeframe.isintraday" in src
    assert "runtime.error(" in src


def test_no_uncertain_builtins_or_input_arithmetic():
    strat = STRATEGY.read_text()
    code = "\n".join(re.sub(r"//.*$", "", ln) for ln in strat.splitlines())
    # strategy() parameters want const arguments; arithmetic on an input is not one.
    assert "commission_value     = input.float(" in code
    assert not re.search(r"\)\s*[/*]\s*[\d.]+\s*,", code), (
        "arithmetic applied to a strategy() argument"
    )
    # drawdown is computed from strategy.equity rather than read from a built-in
    assert "max_drawdown_percent" not in code
    assert "peakEquity" in code


# ------------------------------------------------------------------- parity
def _pine_div(a, b):
    """Pine's `/`: int/int is integer division, everything else is float."""
    if isinstance(a, int) and isinstance(b, int):
        return a // b if b else 0
    return a / b


def _run_transcribed_indicator(bars, cost_bps: float = 0.0) -> dict:
    """Literal transcription of the indicator's `if inRange and hasPrev` block."""
    cost = cost_bps / 10000.0
    eq_on = eq_id = eq_cc = 1.0
    n_bars = 0          # var int
    sum_on = sum_sq_on = 0.0
    n_win_on = 0        # var int
    worst_on = 0.0
    peak_on = 1.0
    max_dd_on = 0.0

    o = bars["open"].to_numpy()
    c = bars["close"].to_numpy()
    for i in range(len(bars)):
        prev_c = c[i - 1] if i > 0 else float("nan")
        has_prev = (i > 0 and np.isfinite(prev_c) and prev_c > 0
                    and np.isfinite(o[i]) and o[i] > 0)
        if not has_prev:
            continue
        r_on = o[i] / prev_c - 1.0
        r_id = c[i] / o[i] - 1.0
        r_cc = c[i] / prev_c - 1.0

        n_bars += 1
        eq_on *= (1.0 + r_on) * (1.0 - cost)
        eq_id *= (1.0 + r_id) * (1.0 - cost)
        eq_cc *= (1.0 + r_cc)
        sum_on += r_on
        sum_sq_on += r_on * r_on
        n_win_on += 1 if r_on > 0 else 0
        worst_on = r_on if n_bars == 1 else min(worst_on, r_on)
        peak_on = max(peak_on, eq_on)
        max_dd_on = min(max_dd_on, eq_on / peak_on - 1.0)

    years = _pine_div(n_bars, 252.0)
    mean_on = _pine_div(sum_on, n_bars)
    var_on = max(_pine_div(sum_sq_on - n_bars * mean_on * mean_on, n_bars - 1), 0.0)
    sd_on = math.sqrt(var_on)
    return {
        "n": n_bars,
        "eq_on": eq_on,
        "eq_id": eq_id,
        "eq_cc": eq_cc,
        "cagr": eq_on ** (1.0 / years) - 1.0 if eq_on > 0 and years > 0 else 0.0,
        "sharpe": mean_on / sd_on * math.sqrt(252.0) if sd_on > 0 else 0.0,
        "max_dd": max_dd_on,
        # The fix: float() cast. Without it Pine's int/int shows 0 % forever.
        "hit": _pine_div(n_win_on, float(n_bars)),
        "mean": mean_on,
        "worst": worst_on,
        "breakeven_bps": (1.0 - math.exp(-(mean_on - 0.5 * var_on))) * 10000.0,
    }


@pytest.fixture(scope="module")
def parity():
    bars = random_walk(4000, seed=1994, start="2005-01-03", overnight_mu=0.0006,
                       overnight_sigma=0.018, intraday_mu=-0.0004, fat_tail_df=4.0)
    return bars, _run_transcribed_indicator(bars), decompose(bars).frame


def test_equity_curves_match(parity):
    _, pine, d = parity
    assert pine["eq_on"] - 1.0 == pytest.approx(compound(d["r_on"]), rel=1e-9)
    assert pine["eq_id"] - 1.0 == pytest.approx(compound(d["r_id"]), rel=1e-9)
    assert pine["eq_cc"] - 1.0 == pytest.approx(compound(d["r_cc"]), rel=1e-9)


def test_statistics_match(parity):
    _, pine, d = parity
    met = M.compute(d["r_on"])
    assert pine["n"] == met.n
    assert pine["cagr"] == pytest.approx(met.cagr, rel=1e-9)
    assert pine["sharpe"] == pytest.approx(met.sharpe, rel=1e-9)
    assert pine["max_dd"] == pytest.approx(met.max_drawdown, rel=1e-9)
    assert pine["mean"] == pytest.approx(met.mean_return, rel=1e-9)
    assert pine["worst"] == pytest.approx(met.worst, rel=1e-12)


def test_hit_rate_is_not_integer_divided(parity):
    """The regression that started this file. A stale `nWinOn / nBars` reads 0."""
    _, pine, d = parity
    met = M.compute(d["r_on"])
    assert pine["hit"] == pytest.approx(met.hit_rate, rel=1e-9)
    assert 0.3 < pine["hit"] < 0.7, "a plausible hit rate, not 0 or 1"
    assert "nWinOn / float(nBars)" in INDICATOR.read_text()


def test_breakeven_approximation_is_close_enough(parity):
    """Pine uses mu - sigma^2/2; Python takes the exact mean of logs."""
    _, pine, d = parity
    exact = breakeven_cost(d["r_on"]).breakeven_bps
    assert pine["breakeven_bps"] == pytest.approx(exact, rel=0.05)


def test_transcription_still_matches_the_source():
    """If the Pine changes, this test must be updated with it rather than silently
    drifting into describing an older version of the script."""
    src = INDICATOR.read_text()
    for expression in (
        "eqOn    := eqOn * (1.0 + r_on) * (1.0 - cost)",
        "eqId    := eqId * (1.0 + r_id) * (1.0 - cost)",
        "eqCc    := eqCc * (1.0 + r_cc)",
        "sumSqOn := sumSqOn + r_on * r_on",
        "worstOn := nBars == 1 ? r_on : math.min(worstOn, r_on)",
        "peakOn  := math.max(peakOn, eqOn)",
        "maxDdOn := math.min(maxDdOn, eqOn / peakOn - 1.0)",
        "r_on = hasPrev ? open  / close[1] - 1.0 : 0.0",
        "r_id = hasPrev ? close / open      - 1.0 : 0.0",
    ):
        assert expression in src, f"transcription is stale: {expression!r} not in the Pine source"


def test_cost_is_applied_to_the_traded_legs_only(parity):
    """Buy & hold does not trade daily, so the per-round-trip cost must not touch it."""
    bars, _, _ = parity
    free = _run_transcribed_indicator(bars, cost_bps=0.0)
    charged = _run_transcribed_indicator(bars, cost_bps=10.0)
    assert charged["eq_cc"] == pytest.approx(free["eq_cc"], rel=1e-12)
    assert charged["eq_on"] < free["eq_on"]
    n = free["n"]
    assert charged["eq_on"] == pytest.approx(free["eq_on"] * (1 - 10e-4) ** n, rel=1e-9)
