"""Walk-forward evaluation of filter overlays.

The problem this solves
-----------------------
Every overlay (trend filter, vol filter, earnings skip ...) has parameters, and
parameters fitted to the whole history *will* look good on the whole history --
that is what fitting means, not evidence of an edge. The reality check's
deflated Sharpe punishes this after the fact; walk-forward prevents it
structurally:

1. The history is cut into consecutive folds.
2. In each fold, every candidate overlay is scored **only on the training
   window** and the winner is chosen there.
3. The winner's returns are then recorded **only on the test window that
   follows**, which it has never seen.
4. The stitched test windows form the out-of-sample curve -- the only curve
   from which a deployment decision may be read.

The comparison that matters is stitched-OOS vs. the unfiltered baseline over
the same sessions. An overlay earns deployment only by beating the baseline
where it was not fitted; the default configuration therefore remains
*no filter*, and this module is the gate any filter has to pass.

Lag safety is inherited, not re-implemented: candidates are built by
:mod:`micronalgo.research.filters`, whose builders shift every indicator so the
value attached to session ``t`` was knowable before entering at the close of
``t-1``. Computing signals over the full history is therefore sound -- the
fold boundary adds no information leak, because no signal value ever uses data
from its own session, let alone a later one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import filters as F
from .engine import BacktestConfig, net_return_series
from .metrics import deflated_sharpe

SignalBuilder = Callable[[pd.DataFrame], pd.Series | None]


@dataclass(frozen=True)
class Candidate:
    name: str
    build: SignalBuilder


def default_candidates() -> list[Candidate]:
    """A deliberately small menu.

    Every entry here inflates the multiple-testing burden that the deflated
    Sharpe must absorb, so the menu holds a handful of a-priori-motivated
    overlays rather than a parameter sweep. "baseline" is the unfiltered
    strategy and must always be present: it is both a legitimate winner and the
    yardstick the stitched result is judged against.
    """
    return [
        Candidate("baseline", lambda bars: None),
        Candidate("trend200", lambda bars: F.trend_filter(bars, window=200)),
        Candidate("vol<80%", lambda bars: F.volatility_filter(bars, window=20, max_annual_vol=0.80)),
        Candidate("calm-prior", lambda bars: F.prior_move_filter(bars, max_abs_move=0.10)),
        Candidate(
            "trend+vol",
            lambda bars: F.combine(
                F.trend_filter(bars, window=200),
                F.volatility_filter(bars, window=20, max_annual_vol=0.80),
            ),
        ),
    ]


@dataclass
class Fold:
    index: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    chosen: str
    train_scores: dict[str, float]
    test_geo_mean: float
    baseline_test_geo_mean: float


@dataclass
class WalkForwardResult:
    folds: list[Fold]
    oos_returns: pd.Series
    baseline_oos_returns: pd.Series
    candidate_names: list[str]
    score: str = "geometric_mean"
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @property
    def oos_total(self) -> float:
        return float(np.expm1(np.log1p(self.oos_returns).sum()))

    @property
    def baseline_total(self) -> float:
        return float(np.expm1(np.log1p(self.baseline_oos_returns).sum()))

    @property
    def switched(self) -> bool:
        return any(f.chosen != "baseline" for f in self.folds)

    def deflated_sharpe_oos(self) -> float:
        return deflated_sharpe(self.oos_returns, max(len(self.candidate_names), 1))

    def fold_table(self) -> pd.DataFrame:
        rows = [
            {
                "fold": f.index,
                "train": f"{f.train_start.date()}..{f.train_end.date()}",
                "test": f"{f.test_start.date()}..{f.test_end.date()}",
                "chosen": f.chosen,
                "test_bps": f.test_geo_mean * 1e4,
                "baseline_bps": f.baseline_test_geo_mean * 1e4,
                "beat_baseline": f.test_geo_mean > f.baseline_test_geo_mean,
            }
            for f in self.folds
        ]
        return pd.DataFrame(rows).set_index("fold")

    def oos_drawdown(self) -> tuple[float, float]:
        """Max drawdown of the stitched out-of-sample curve and of the baseline."""
        def _dd(series: pd.Series) -> float:
            equity = (1.0 + series.fillna(0.0)).cumprod().to_numpy()
            if equity.size == 0:
                return 0.0
            peak = np.maximum.accumulate(equity)
            return float(np.min(equity / peak - 1.0))

        return _dd(self.oos_returns), _dd(self.baseline_oos_returns)

    def verdict(self) -> str:
        margin = self.oos_total - self.baseline_total
        wins = sum(f.test_geo_mean > f.baseline_test_geo_mean for f in self.folds)
        dd_chosen, dd_base = self.oos_drawdown()
        header = (
            f"out-of-sample (selected on {self.score}): "
            f"chosen {self.oos_total:+.2%} vs baseline {self.baseline_total:+.2%} "
            f"({wins}/{len(self.folds)} folds beat the baseline)\n"
            f"out-of-sample drawdown: chosen {dd_chosen:.1%} vs baseline {dd_base:.1%}"
        )
        if not self.switched:
            return header + (
                "\nEvery fold chose the unfiltered baseline: none of the overlays looked better "
                "even in-sample. Trade the base strategy or nothing; there is no overlay decision "
                "to make."
            )
        if margin <= 0:
            return header + (
                "\nThe overlays looked better in the training windows and then FAILED to beat the "
                "baseline where it counted. That is the signature of overfitting, and it is exactly "
                "what this procedure exists to catch. Deploy no filter."
            )
        if wins <= len(self.folds) / 2:
            return header + (
                "\nThe stitched result is ahead, but in most folds it was not -- the margin comes "
                "from a minority of windows, which is fragile. Deploy no filter unless a longer "
                "history changes this picture."
            )
        return header + (
            "\nThe chosen overlays beat the baseline out-of-sample and in most folds. That is the "
            "minimum evidence for deploying a filter -- keep re-running this as new data arrives, "
            "and treat a deteriorating margin as the exit signal for the overlay."
        )


def _geo_mean(returns: pd.Series) -> float:
    """Per-session geometric mean.

    Chosen over the arithmetic mean for the same reason as everywhere else in
    this project: the strategy compounds, and the variance drag is on the order
    of the edge itself. A wiped window scores -1, the worst possible.
    """
    r = returns.dropna().to_numpy(dtype="float64")
    if r.size == 0:
        return float("-inf")
    gross = 1.0 + r
    if np.any(gross <= 0):
        return -1.0
    return float(np.expm1(np.mean(np.log(gross))))


def _calmar(returns: pd.Series) -> float:
    """Compound growth divided by the depth of the worst drawdown in the window.

    The score to select on when the problem is not "is there an edge" but "can
    this be held". Selecting on return alone will happily pick an overlay that
    earns more and hurts more; Calmar will not. A window with no drawdown at all
    falls back to the geometric mean, since dividing by zero would make an
    untested overlay look infinitely good.
    """
    r = returns.dropna().to_numpy(dtype="float64")
    if r.size == 0:
        return float("-inf")
    gross = 1.0 + r
    if np.any(gross <= 0):
        return -1.0
    equity = np.cumprod(gross)
    peak = np.maximum.accumulate(equity)
    depth = float(np.min(equity / peak - 1.0))
    growth = float(np.expm1(np.mean(np.log(gross))))
    if depth >= -1e-12:
        return growth
    return growth / abs(depth)


SCORES: dict[str, Callable[[pd.Series], float]] = {
    "geometric_mean": _geo_mean,
    "calmar": _calmar,
}


def walk_forward(
    bars: pd.DataFrame,
    *,
    candidates: Sequence[Candidate] | None = None,
    config: BacktestConfig | None = None,
    train_sessions: int = 756,
    test_sessions: int = 252,
    min_tail_sessions: int = 21,
    score: str = "geometric_mean",
) -> WalkForwardResult:
    """Run the walk-forward protocol.

    Rolling folds: train on ``train_sessions``, test on the following
    ``test_sessions``, then slide by ``test_sessions``. A leftover tail shorter
    than a full test window becomes one final, shorter fold (down to
    ``min_tail_sessions``) rather than being dropped -- the newest data is
    precisely the data a deployment decision most needs, and silently ignoring
    up to a year of it would make the verdict describe the past instead of the
    present. Every out-of-sample day is used exactly once and never re-used for
    selection.
    """
    candidates = list(candidates) if candidates is not None else default_candidates()
    if not any(c.name == "baseline" for c in candidates):
        raise ValueError("candidates must include a 'baseline' entry (the unfiltered strategy)")
    if score not in SCORES:
        raise KeyError(f"unknown score {score!r}; have {sorted(SCORES)}")
    score_fn = SCORES[score]
    config = config or BacktestConfig()

    # Net return series per candidate, computed once over the full history.
    # Signals are lag-safe by construction (see module docstring), so slicing
    # these series by fold leaks nothing.
    net: dict[str, pd.Series] = {}
    for cand in candidates:
        signal = cand.build(bars)
        net[cand.name] = net_return_series(bars, config, signal=signal)

    index = net["baseline"].index
    n = len(index)
    if n < train_sessions + test_sessions:
        raise ValueError(
            f"need at least train+test = {train_sessions + test_sessions} sessions, have {n}"
        )

    folds: list[Fold] = []
    oos_parts: list[pd.Series] = []
    base_parts: list[pd.Series] = []

    def _add_fold(fold_no: int, start: int, test_len: int) -> None:
        tr = slice(start, start + train_sessions)
        te = slice(start + train_sessions, start + train_sessions + test_len)

        scores = {name: score_fn(series.iloc[tr]) for name, series in net.items()}
        chosen = max(scores, key=lambda k: scores[k])

        test_chunk = net[chosen].iloc[te]
        base_chunk = net["baseline"].iloc[te]
        oos_parts.append(test_chunk)
        base_parts.append(base_chunk)

        folds.append(
            Fold(
                index=fold_no,
                train_start=index[tr][0],
                train_end=index[tr][-1],
                test_start=index[te][0],
                test_end=index[te][-1],
                chosen=chosen,
                train_scores=scores,
                test_geo_mean=_geo_mean(test_chunk),
                baseline_test_geo_mean=_geo_mean(base_chunk),
            )
        )

    fold_no = 0
    start = 0
    while start + train_sessions + test_sessions <= n:
        _add_fold(fold_no, start, test_sessions)
        fold_no += 1
        start += test_sessions

    # The tail fold: whatever newest data did not fill a whole test window.
    tail = n - (start + train_sessions)
    if tail >= min_tail_sessions:
        _add_fold(fold_no, start, tail)

    if not folds:
        raise ValueError("no complete fold fits the data")

    return WalkForwardResult(
        folds=folds,
        oos_returns=pd.concat(oos_parts),
        baseline_oos_returns=pd.concat(base_parts),
        candidate_names=[c.name for c in candidates],
        score=score,
    )
