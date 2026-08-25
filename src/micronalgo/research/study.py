"""The full study, computed once into a serialisable result.

Separating *computing* the study from *rendering* it means the same numbers feed
the console output, the HTML report and the tests -- so a figure quoted in the
report cannot drift from the figure a test asserts.

The reality check
-----------------
:class:`RealityCheck` is not optional decoration. The headline number for this
strategy is spectacular and almost entirely uninformative, and a report that
shows it without the following would be a sales document:

* what the edge is **after costs**, under several execution assumptions;
* whether it still exists in the **recent** regime, or died with the wide spreads;
* whether a **bootstrap** confidence interval for the mean excludes zero;
* how much of it is **a handful of sessions**;
* what the **worst overnight gap** was, since that is the risk actually borne;
* the **deflated** Sharpe, given how many variants were examined.

Each produces a verdict against a stated threshold, and the overall verdict is
the worst of them.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from ..data.validate import ValidationReport
from . import metrics as M
from . import robustness as R
from .costs import ALL_SCENARIOS, BreakEven, breakeven_cost, cost_drag_table, scenario
from .engine import BacktestConfig, BacktestResult, simulate
from .returns import Decomposition, compound, decompose


class Verdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"

    @property
    def rank(self) -> int:
        return {"PASS": 0, "WARN": 1, "FAIL": 2}[self.value]


@dataclass
class Criterion:
    name: str
    verdict: Verdict
    value: str
    threshold: str
    explanation: str

    def __str__(self) -> str:
        return f"[{self.verdict.value:<4}] {self.name:<34} {self.value:>22}   (threshold: {self.threshold})"


@dataclass
class RealityCheck:
    criteria: list[Criterion] = field(default_factory=list)

    def add(self, name: str, verdict: Verdict, value: str, threshold: str, explanation: str) -> None:
        self.criteria.append(Criterion(name, verdict, value, threshold, explanation))

    @property
    def verdict(self) -> Verdict:
        if not self.criteria:
            return Verdict.WARN
        return max((c.verdict for c in self.criteria), key=lambda v: v.rank)

    @property
    def summary(self) -> str:
        return {
            Verdict.PASS: (
                "Every reality check passed. That is not permission to trade real money -- it means "
                "the hypothesis survived the tests in this repository on this data. Paper-trade it for "
                "at least a full quarter and compare live fills against the backtest before going further."
            ),
            Verdict.WARN: (
                "The effect is measurable but at least one check flagged a real weakness. Read the WARN "
                "lines: they usually mean the result depends on a few sessions, on an old regime, or on "
                "an execution assumption that has to hold exactly. Paper trading is the only sensible "
                "next step."
            ),
            Verdict.FAIL: (
                "At least one check failed outright. On this data the strategy should not be traded, "
                "with real money or otherwise, beyond curiosity. The failing line says which assumption "
                "does not hold."
            ),
        }[self.verdict]


@dataclass
class StudyResult:
    symbol: str
    provenance: str
    start: dt.date
    end: dt.date
    n_sessions: int
    decomposition: Decomposition
    headline: dict[str, float]
    by_scenario: pd.DataFrame
    metric_table: pd.DataFrame
    subperiods_regime: pd.DataFrame
    subperiods_decade: pd.DataFrame
    day_of_week: pd.DataFrame
    drop_best: pd.DataFrame
    bootstrap: R.BootstrapResult
    permutation: dict[str, float]
    breakeven: BreakEven
    cost_drag: pd.DataFrame
    monte_carlo: pd.DataFrame
    worst_gaps: pd.DataFrame
    drawdowns: pd.DataFrame
    bounce: dict[str, float]
    spread_by_era: pd.DataFrame
    reality: RealityCheck
    validation: ValidationReport | None = None
    n_variants_examined: int = 1
    rolling: pd.DataFrame = field(default_factory=pd.DataFrame)
    results: dict[str, BacktestResult] = field(default_factory=dict)

    def headline_line(self) -> str:
        h = self.headline
        return (
            f"overnight {h['overnight'] * 100:>+16,.1f}%   "
            f"intraday {h['intraday'] * 100:>+10,.1f}%   "
            f"buy&hold {h['buyhold'] * 100:>+12,.1f}%"
        )


def run_study(
    bars: pd.DataFrame,
    *,
    symbol: str = "MU",
    provenance: str = "",
    validation: ValidationReport | None = None,
    cost_scenarios: tuple[str, ...] = ALL_SCENARIOS,
    primary_scenario: str = "auction-retail",
    initial_capital: float = 100_000.0,
    n_variants_examined: int = 1,
    bootstrap_resamples: int = 2000,
    recent_years: int = 5,
    on_progress: Callable[[str], None] | None = None,
) -> StudyResult:
    """Compute everything the report needs.

    ``on_progress`` receives one short line per phase. Silence during a
    multi-second computation reads as a hang, and a user who interrupts a run
    gets no result at all -- so the phases announce themselves.
    """
    say = on_progress or (lambda _msg: None)
    if primary_scenario not in cost_scenarios:
        # Every downstream section (monte carlo, drawdown chart, reality check)
        # reads results[primary_scenario]; a custom scenario list that omits it
        # would KeyError three functions later with no hint why.
        cost_scenarios = (*cost_scenarios, primary_scenario)

    say(f"decomposing {len(bars):,} sessions")
    dec = decompose(bars)
    frame = dec.frame
    r_on, r_id, r_cc = frame["r_on"], frame["r_id"], frame["r_cc"]

    headline = {
        "overnight": compound(r_on),
        "intraday": compound(r_id),
        "buyhold": compound(r_cc),
        "identity_error": dec.max_identity_error,
    }

    # --- cost scenarios -----------------------------------------------------
    rows = []
    results: dict[str, BacktestResult] = {}
    say(f"backtesting {len(cost_scenarios)} cost scenarios")
    for name in cost_scenarios:
        cfg = BacktestConfig(mode="overnight", cost=scenario(name), initial_capital=initial_capital)
        res = simulate(bars, cfg)
        results[name] = res
        met = M.compute(res.returns, equity=res.equity)
        rows.append(
            {
                "scenario": name,
                "total_return": res.total_return,
                "cagr": met.cagr,
                "sharpe": met.sharpe,
                "max_drawdown": met.max_drawdown,
                "final_equity": float(res.equity.iloc[-1]) if len(res.equity) else np.nan,
                "total_costs": res.total_cost_cash,
                # Cost as a fraction of the notional actually traded, averaged over
                # trades. Normalising by the *initial* capital would inflate this by
                # the whole compounding factor, since late trades are far larger.
                "cost_per_trade_bps": _mean_cost_bps(res),
            }
        )
    by_scenario = pd.DataFrame(rows).set_index("scenario")

    for mode in ("intraday", "buyhold"):
        results[mode] = simulate(
            bars, BacktestConfig(mode=mode, cost=scenario(primary_scenario), initial_capital=initial_capital)
        )

    metric_table = M.compare({"overnight": r_on, "intraday": r_id, "buy&hold": r_cc})

    # --- robustness ---------------------------------------------------------
    say(f"stationary bootstrap, {bootstrap_resamples:,} resamples (the slow part)")
    bootstrap = R.stationary_bootstrap(r_on, statistic="mean", n_resamples=bootstrap_resamples)
    permutation = R.circular_permutation_test(r_on, r_id, n_resamples=2000)
    be = breakeven_cost(r_on)
    drop_best = R.drop_best_days(r_on)
    subs_regime = R.subperiods(r_on, by="regime")
    subs_decade = R.subperiods(r_on, by="decade")
    dow = R.day_of_week(r_on)
    say("monte carlo, 800 resampled futures")
    mc = R.monte_carlo_paths(results[primary_scenario].returns, n_paths=800)
    rolling = R.rolling_stats(r_on, window=252)

    worst = frame.nsmallest(10, "r_on")[["r_on", "r_id", "r_cc"]].copy()
    worst.index = worst.index.date

    say("spread and bid-ask-bounce diagnostics")
    drawdowns = M.drawdown_episodes(results[primary_scenario].equity, top=5)
    bounce = R.bid_ask_bounce_check(bars, r_on)
    spreads = R.spread_by_era(bars)

    reality = _reality_check(
        r_on=r_on,
        primary=results[primary_scenario],
        by_scenario=by_scenario,
        primary_scenario=primary_scenario,
        bootstrap=bootstrap,
        breakeven=be,
        drop_best=drop_best,
        validation=validation,
        n_variants=n_variants_examined,
        recent_years=recent_years,
        bounce=bounce,
    )

    return StudyResult(
        symbol=symbol,
        provenance=provenance,
        start=bars.index[0].date(),
        end=bars.index[-1].date(),
        n_sessions=len(frame),
        decomposition=dec,
        headline=headline,
        by_scenario=by_scenario,
        metric_table=metric_table,
        subperiods_regime=subs_regime,
        subperiods_decade=subs_decade,
        day_of_week=dow,
        drop_best=drop_best,
        bootstrap=bootstrap,
        permutation=permutation,
        breakeven=be,
        cost_drag=cost_drag_table([0.3, 0.5, 1, 2, 5, 10, 25, 50]),
        monte_carlo=mc,
        worst_gaps=worst,
        drawdowns=drawdowns,
        bounce=bounce,
        spread_by_era=spreads,
        reality=reality,
        validation=validation,
        n_variants_examined=n_variants_examined,
        rolling=rolling,
        results=results,
    )


def _mean_cost_bps(res: BacktestResult) -> float:
    """Average round-trip cost in basis points of the notional traded."""
    if res.trades.empty or "notional" not in res.trades.columns:
        return float("nan")
    notional = res.trades["notional"].to_numpy(dtype="float64")
    cost = res.trades["cost"].to_numpy(dtype="float64")
    mask = np.isfinite(notional) & (notional > 0) & np.isfinite(cost)
    if not mask.any():
        return float("nan")
    return float(np.mean(cost[mask] / notional[mask]) * 1e4)


def _reality_check(
    *,
    r_on: pd.Series,
    primary: BacktestResult,
    by_scenario: pd.DataFrame,
    primary_scenario: str,
    bootstrap: R.BootstrapResult,
    breakeven: BreakEven,
    drop_best: pd.DataFrame,
    validation: ValidationReport | None,
    n_variants: int,
    recent_years: int,
    bounce: dict[str, float] | None = None,
) -> RealityCheck:
    rc = RealityCheck()

    # 1. Data integrity gates everything else.
    if validation is not None:
        rc.add(
            "data validation",
            Verdict.PASS if validation.ok else Verdict.FAIL,
            f"{len(validation.errors)} errors, {len(validation.warnings)} warnings",
            "zero errors",
            "A mis-adjusted price series produces a beautiful and entirely fictional overnight curve. "
            "Nothing below means anything if this line fails.",
        )

    # 2. Does it survive realistic costs?
    net_cagr = float(by_scenario.loc[primary_scenario, "cagr"])
    rc.add(
        f"CAGR after costs ({primary_scenario})",
        Verdict.PASS if net_cagr > 0.02 else (Verdict.WARN if net_cagr > 0 else Verdict.FAIL),
        f"{net_cagr:.2%}",
        "> 2 %/yr",
        "The strategy trades ~252 round trips a year, so cost assumptions dominate. This line uses "
        "auction (MOC/OPG) execution at retail size.",
    )

    # 3. Does it survive an execution assumption failure?
    if "pessimistic-5bp" in by_scenario.index:
        pess = float(by_scenario.loc["pessimistic-5bp", "cagr"])
        rc.add(
            "CAGR at 5 bps/side slippage",
            Verdict.PASS if pess > 0 else Verdict.WARN,
            f"{pess:.2%}",
            "> 0 %/yr",
            "What happens if the auction fill is not achieved and a spread must be crossed. If this is "
            "deeply negative the strategy is a bet on execution quality, not on the anomaly.",
        )

    # 4. Break-even cost vs. what execution actually costs.
    rc.add(
        "break-even round-trip cost",
        Verdict.PASS if breakeven.breakeven_bps > 2.0 else (
            Verdict.WARN if breakeven.breakeven_bps > 0.5 else Verdict.FAIL
        ),
        f"{breakeven.breakeven_bps:.2f} bps",
        "> 2 bps",
        f"Computed geometrically. The arithmetic mean is {breakeven.arithmetic_mean * 1e4:.2f} bps but "
        f"volatility drag removes {breakeven.variance_drag * 1e4:.2f} bps of it every session -- that gap "
        "is what makes naive averages of daily returns so misleading.",
    )

    # 4b. Could the whole thing be which side of the quote prints where?
    if bounce:
        ratio = bounce.get("edge_over_spread", float("nan"))
        spread_bps = bounce.get("corwin_schultz_spread", float("nan")) * 1e4
        determinable = bool(bounce.get("determinable", False))
        neg = bounce.get("cs_negative_share", float("nan"))

        if not determinable:
            # The estimator could not measure. That is NOT a failure of the
            # strategy, and reporting it as one would be worse than not checking
            # at all: it manufactures a verdict from noise and discredits every
            # other line. But it is not an all-clear either -- the question is
            # simply open, and the era table below is what carries the evidence.
            rc.add(
                "edge vs. effective spread",
                Verdict.WARN,
                "not measurable from daily bars",
                "needs quote or minute data",
                "The concern is real: if closing prints land on the bid and opening prints on the "
                "ask, a full effective spread appears as overnight 'return' every session with no "
                "economics behind it. But the only estimator available from daily bars "
                "(Corwin-Schultz) has a noise floor that scales with volatility, and on this series "
                f"{neg:.0%} of its per-day estimates are negative -- i.e. it is averaging symmetric "
                f"noise, and the {spread_bps:.0f} bps it reports is that noise, not a spread. This "
                "line therefore says 'unknown', which is the honest answer. What settles it: quote "
                "or minute data, and the era table -- a spread artefact must shrink by two orders "
                "of magnitude from the 1/8-tick era to decimalisation, so an edge that stays flat "
                "across those eras is hard to explain that way.",
            )
        elif np.isfinite(ratio):
            rc.add(
                "edge vs. effective spread",
                Verdict.PASS if ratio > 2.0 else (Verdict.WARN if ratio > 1.0 else Verdict.FAIL),
                f"{ratio:.2f}x  (edge {bounce['mean_overnight'] * 1e4:.2f} bps vs spread {spread_bps:.1f} bps)",
                "> 2x the spread",
                "If closing prints land on the bid and opening prints on the ask, a full effective "
                "spread appears as overnight 'return' every session with no economics behind it at "
                "all. The estimator reported a resolvable spread here, so the comparison means "
                "something.",
            )

    # 5. Is the mean statistically distinguishable from zero?
    ci_excludes_zero = bootstrap.ci_low > 0
    rc.add(
        "bootstrap 95 % CI for mean",
        Verdict.PASS if ci_excludes_zero else Verdict.WARN,
        f"[{bootstrap.ci_low * 1e4:.2f}, {bootstrap.ci_high * 1e4:.2f}] bps",
        "lower bound > 0",
        f"Stationary bootstrap, block length {bootstrap.block_length:.0f}, "
        f"{bootstrap.n_resamples} resamples. A plain t-test would overstate significance here because "
        "overnight returns are fat-tailed and serially dependent.",
    )

    # 6. Has it decayed?
    cutoff = r_on.index[-1] - pd.DateOffset(years=recent_years)
    recent = r_on[r_on.index >= cutoff]
    if len(recent) > 100:
        recent_mean = float(recent.mean())
        recent_total = compound(recent)
        rc.add(
            f"last {recent_years} years",
            Verdict.PASS if recent_mean > 0 and recent_total > 0 else Verdict.FAIL,
            f"{recent_mean * 1e4:+.2f} bps/session, {recent_total:+.1%} total",
            "positive",
            "The single most important robustness test for this anomaly. Much of the historical effect "
            "sits in the wide-spread era before decimalisation in 2001. If it is gone now, the headline "
            "number is history, not a strategy.",
        )

    # 7. Is it a handful of sessions?
    top10 = M.tail_concentration(r_on, 10)
    rc.add(
        "top 10 sessions' share",
        Verdict.PASS if top10 < 0.35 else (Verdict.WARN if top10 < 0.75 else Verdict.FAIL),
        f"{top10:.1%} of total log return",
        "< 35 %",
        "If a decade of edge is 10 sessions, the expected return going forward is dominated by whether "
        "those sessions recur -- which is a lottery, not a premium.",
    )

    if 10 in drop_best.index:
        dropped = float(drop_best.loc[10, "total_return"])
        rc.add(
            "total return, best 10 removed",
            Verdict.PASS if dropped > 0 else Verdict.WARN,
            f"{dropped:+.1%}",
            "> 0 %",
            "Same question from the other side: delete the ten luckiest sessions and see what is left.",
        )

    # 8. Multiple testing.
    dsr = M.deflated_sharpe(r_on, n_variants)
    rc.add(
        "deflated Sharpe",
        Verdict.PASS if (dsr == dsr and dsr > 0.95) else (Verdict.WARN if (dsr == dsr and dsr > 0.5) else Verdict.FAIL),
        f"{dsr:.3f}" if dsr == dsr else "n/a",
        "> 0.95",
        f"Probability the true Sharpe beats what {n_variants} random variants would produce by luck. "
        "This number is only honest if n_variants counts every configuration you looked at, including "
        "the discarded ones.",
    )

    # 9. Risk actually borne.
    met = M.compute(primary.returns, equity=primary.equity)
    rc.add(
        "max drawdown",
        Verdict.PASS if met.max_drawdown > -0.30 else (Verdict.WARN if met.max_drawdown > -0.50 else Verdict.FAIL),
        f"{met.max_drawdown:.1%} over {met.max_drawdown_days} sessions",
        "> -30 %",
        "You hold every earnings gap overnight. The drawdown is the number that decides whether a human "
        "actually stays in the trade, which is the assumption every backtest quietly makes.",
    )

    worst_gap = float(r_on.min())
    rc.add(
        "worst single overnight",
        Verdict.PASS if worst_gap > -0.15 else (Verdict.WARN if worst_gap > -0.30 else Verdict.FAIL),
        f"{worst_gap:.1%}",
        "> -15 %",
        "There is no stop-loss overnight. Whatever the gap is, you take it in full -- and with leverage "
        "a single one of these ends the account.",
    )

    return rc


def to_dict(result: StudyResult) -> dict:
    """JSON-serialisable summary, for regression tests and machine consumers."""
    return {
        "symbol": result.symbol,
        "start": str(result.start),
        "end": str(result.end),
        "n_sessions": result.n_sessions,
        "headline": {k: float(v) for k, v in result.headline.items()},
        "by_scenario": {
            k: {kk: (None if pd.isna(vv) else float(vv)) for kk, vv in v.items()}
            for k, v in result.by_scenario.to_dict("index").items()
        },
        "breakeven_bps": result.breakeven.breakeven_bps,
        "bootstrap": {
            "observed": result.bootstrap.observed,
            "ci_low": result.bootstrap.ci_low,
            "ci_high": result.bootstrap.ci_high,
            "p_value": result.bootstrap.p_value_gt_zero,
        },
        "bid_ask_bounce": {k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                           for k, v in result.bounce.items()},
        "verdict": result.reality.verdict.value,
        "criteria": [dataclasses.asdict(c) | {"verdict": c.verdict.value} for c in result.reality.criteria],
    }
