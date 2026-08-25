"""Performance and risk metrics.

Three metrics here exist specifically because the naive ones lie about *this*
strategy:

* **Probabilistic Sharpe Ratio (PSR)** -- MU overnight returns are strongly
  non-normal (earnings gaps). A plain Sharpe assumes normality; PSR corrects the
  standard error for skew and excess kurtosis (Bailey & Lopez de Prado 2012).
* **Deflated Sharpe Ratio (DSR)** -- if you tried ``N`` variants and reported the
  best, the winner's Sharpe is biased upward by roughly the expected maximum of
  ``N`` draws. DSR subtracts that. Any parameter search must report it.
* **Tail concentration** -- if the entire edge lives in 10 of 7,800 sessions,
  it is a lottery ticket rather than a premium, and no amount of Sharpe changes
  that.

Annualisation uses 252 sessions. That is correct here even though the overnight
strategy is only *exposed* ~17.5 hours a week: it takes one position per session,
so the natural period is the session. The low exposure is reported separately as
``exposure_fraction`` -- it is the reason the risk-adjusted numbers can look
spectacular while the absolute numbers need leverage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd
from scipy import stats

TRADING_DAYS = 252
HOURS_PER_SESSION_OVERNIGHT = 17.5
HOURS_PER_WEEK = 168.0


@dataclass
class Metrics:
    n: int
    years: float
    total_return: float
    cagr: float
    ann_vol: float
    sharpe: float
    sortino: float
    max_drawdown: float
    max_drawdown_days: int
    calmar: float
    hit_rate: float
    mean_return: float
    median_return: float
    geo_mean_return: float
    skew: float
    excess_kurtosis: float
    best: float
    worst: float
    var_95: float
    cvar_95: float
    var_99: float
    cvar_99: float
    psr_vs_zero: float
    t_stat: float
    exposure_fraction: float = 1.0
    top10_share_of_logsum: float = float("nan")
    extra: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        extra = d.pop("extra")
        d.update(extra)
        return d


def _clean(returns: pd.Series | np.ndarray) -> np.ndarray:
    r = np.asarray(returns, dtype="float64")
    return r[np.isfinite(r)]


def max_drawdown(equity: pd.Series | np.ndarray) -> tuple[float, int]:
    """Maximum peak-to-trough drawdown and the longest underwater run in periods."""
    eq = np.asarray(equity, dtype="float64")
    if eq.size == 0:
        return 0.0, 0
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, eq / peak - 1.0, 0.0)
    mdd = float(np.nanmin(dd)) if dd.size else 0.0

    underwater = eq < peak
    longest = run = 0
    for flag in underwater:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    return mdd, int(longest)


def position_sizing_table(
    returns: pd.Series,
    fractions: tuple[float, ...] = (1.0, 0.75, 0.5, 0.35, 0.25, 0.15),
    *,
    periods_per_year: int = TRADING_DAYS,
) -> pd.DataFrame:
    """What each deployment fraction does to return AND to drawdown.

    The honest way to respond to a drawdown you cannot stomach is to trade
    smaller, not to change the threshold. Deploying a fraction ``f`` of capital
    scales every per-session return by ``f``, so the whole equity path is
    re-derived exactly rather than approximated -- drawdown does not scale
    linearly with ``f`` and a rule of thumb would understate it.

    Reported per fraction: CAGR, max drawdown, Calmar, and the worst single
    session. Calmar is the column that matters when choosing: it barely moves
    across sizes, which is the point -- sizing down buys survivability at a
    proportional cost in return, and does not improve the trade's quality.

    This does not model leverage above 1.0. Above that, borrowing costs and the
    ruin risk of a single overnight gap dominate, and neither is captured by
    scaling a return series.
    """
    r = returns.dropna().astype("float64")
    columns = ["fraction", "cagr", "max_drawdown", "calmar", "worst_session",
               "sessions_under_water"]
    if r.empty:
        # Six rows of NaN would look like an answer. There isn't one.
        return pd.DataFrame(columns=columns).set_index("fraction")
    rows = []
    for f in fractions:
        if f <= 0:
            continue
        scaled = r * f
        equity = (1.0 + scaled).cumprod()
        n = len(scaled)
        years = n / periods_per_year
        total = float(equity.iloc[-1] - 1.0) if n else 0.0
        cagr = _cagr(total, years)
        mdd, under = max_drawdown(equity)
        rows.append(
            {
                "fraction": f,
                "cagr": cagr,
                "max_drawdown": mdd,
                "calmar": (cagr / abs(mdd)) if mdd < 0 and np.isfinite(cagr) else float("nan"),
                "worst_session": float(scaled.min()) if n else float("nan"),
                "sessions_under_water": under,
            }
        )
    return pd.DataFrame(rows).set_index("fraction")


def _cagr(total_return: float, years: float) -> float:
    """Compound annual growth, with a wiped account reported as -100 %.

    ``(1 + total) ** (1 / years)`` is undefined at or below zero, and returning
    NaN there prints as "n/a" -- which is the one place vagueness is unaffordable,
    because it hides a total loss behind a formatting artefact.
    """
    if years <= 0:
        return float("nan")
    gross = 1.0 + total_return
    if gross <= 0.0:
        return -1.0
    return float(gross ** (1.0 / years) - 1.0)


def drawdown_episodes(equity: pd.Series, top: int = 5) -> pd.DataFrame:
    """The largest drawdowns, each with when it started, bottomed and recovered.

    A single "max drawdown: -54 %" says almost nothing a person can act on. The
    questions that decide whether a strategy is holdable are *when* and *how
    long*: a -54 % episode that happened once in 2008 and recovered in a year is
    a different proposition from one that recurs every three years, even though
    both print the same headline number.

    An episode runs from the equity peak, through the trough, to the session
    that first exceeds the old peak. An episode still under water at the end of
    the sample has no recovery date and is reported as ongoing -- silently
    treating it as recovered would be the flattering error.
    """
    eq = equity.dropna().astype("float64")
    if eq.empty:
        return pd.DataFrame(
            columns=["start", "trough", "recovered", "depth", "sessions_to_trough",
                     "sessions_to_recover", "ongoing"]
        )

    values = eq.to_numpy()
    index = eq.index
    peaks = np.maximum.accumulate(values)
    under = values < peaks

    episodes: list[dict] = []
    i = 0
    n = len(values)
    while i < n:
        if not under[i]:
            i += 1
            continue
        start_pos = i - 1 if i > 0 else 0        # the peak the fall began from
        j = i
        while j < n and under[j]:
            j += 1
        segment = values[i:j]
        trough_off = int(np.argmin(segment))
        trough_pos = i + trough_off
        peak_value = values[start_pos]
        recovered = j < n
        episodes.append(
            {
                "start": index[start_pos],
                "trough": index[trough_pos],
                "recovered": index[j] if recovered else pd.NaT,
                "depth": float(values[trough_pos] / peak_value - 1.0),
                "sessions_to_trough": trough_pos - start_pos,
                "sessions_to_recover": (j - start_pos) if recovered else (n - start_pos),
                "ongoing": not recovered,
            }
        )
        i = j

    if not episodes:
        return pd.DataFrame(
            columns=["start", "trough", "recovered", "depth", "sessions_to_trough",
                     "sessions_to_recover", "ongoing"]
        )
    frame = pd.DataFrame(episodes).sort_values("depth").head(top).reset_index(drop=True)
    for col in ("start", "trough", "recovered"):
        frame[col] = pd.to_datetime(frame[col]).dt.date
    return frame


def probabilistic_sharpe(sr: float, n: int, skew: float, excess_kurt: float, benchmark_sr: float = 0.0) -> float:
    """P(true Sharpe > ``benchmark_sr``) given the sample's higher moments.

    ``sr`` and ``benchmark_sr`` are **per-period** (not annualised) Sharpe ratios.
    """
    if n < 3:
        return float("nan")
    denom = 1.0 - skew * sr + (excess_kurt / 4.0) * sr**2
    if denom <= 0:
        return float("nan")
    z = (sr - benchmark_sr) * np.sqrt(n - 1) / np.sqrt(denom)
    return float(stats.norm.cdf(z))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum per-period Sharpe from ``n_trials`` independent draws.

    Uses the standard extreme-value approximation from Bailey & Lopez de Prado.
    """
    if n_trials <= 1 or sr_variance <= 0:
        return 0.0
    gamma = 0.5772156649015329  # Euler-Mascheroni
    e = np.e
    z1 = stats.norm.ppf(1.0 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1.0 - 1.0 / (n_trials * e))
    return float(np.sqrt(sr_variance) * ((1.0 - gamma) * z1 + gamma * z2))


def deflated_sharpe(
    returns: pd.Series | np.ndarray, n_trials: int, *, trial_sharpes: np.ndarray | None = None
) -> float:
    """Deflated Sharpe Ratio: PSR against the Sharpe a lucky search would produce.

    ``n_trials`` must be the honest count of every variant considered, including
    the ones that were discarded. Passing 1 turns this back into PSR-vs-zero and
    defeats the purpose.
    """
    r = _clean(returns)
    if r.size < 3:
        return float("nan")
    sd = np.std(r, ddof=1)
    if sd == 0:
        return float("nan")
    sr = float(np.mean(r) / sd)
    var_sr = float(np.var(trial_sharpes, ddof=1)) if trial_sharpes is not None and len(trial_sharpes) > 1 else (
        (1.0 + 0.5 * sr**2) / max(r.size - 1, 1)
    )
    sr0 = expected_max_sharpe(n_trials, var_sr)
    return probabilistic_sharpe(sr, r.size, float(stats.skew(r)), float(stats.kurtosis(r)), benchmark_sr=sr0)


def tail_concentration(returns: pd.Series | np.ndarray, top: int = 10) -> float:
    """Share of the total *log* return contributed by the ``top`` best sessions.

    Log space is the right space: log returns add, so "these 10 days made 60 % of
    the total" is a statement that actually decomposes. A value near or above 1.0
    means the strategy is a handful of lucky sessions wearing a trend coat.
    """
    r = _clean(returns)
    if r.size == 0:
        return float("nan")
    gross = 1.0 + r
    gross = gross[gross > 0]
    if gross.size == 0:
        return float("nan")
    logs = np.log(gross)
    total = float(np.sum(logs))
    if abs(total) < 1e-15:
        return float("nan")
    k = min(top, logs.size)
    return float(np.sum(np.sort(logs)[-k:]) / total)


def compute(
    returns: pd.Series,
    *,
    equity: pd.Series | None = None,
    periods_per_year: int = TRADING_DAYS,
    exposure_fraction: float = 1.0,
    risk_free_annual: float = 0.0,
) -> Metrics:
    """Full metric set for a per-session return series."""
    r_series = returns.astype("float64").dropna()
    r = r_series.to_numpy()
    n = r.size
    if n == 0:
        # An empty or all-NaN series (every session filtered out) must yield an
        # explicit empty result, not a crash. Spelled out field by field rather
        # than star-unpacked: a positional splat silently breaks the moment the
        # dataclass grows a field, which is exactly what happened once.
        zero = float("nan")
        return Metrics(
            n=0, years=0.0, total_return=0.0, cagr=zero, ann_vol=zero, sharpe=zero,
            sortino=zero, max_drawdown=0.0, max_drawdown_days=0, calmar=zero,
            hit_rate=zero, mean_return=zero, median_return=zero, geo_mean_return=zero,
            skew=zero, excess_kurtosis=zero, best=zero, worst=zero, var_95=zero,
            cvar_95=zero, var_99=zero, cvar_99=zero, psr_vs_zero=zero, t_stat=zero,
            exposure_fraction=exposure_fraction,
        )

    if equity is None:
        equity = (1.0 + r_series).cumprod()
    eq = equity.to_numpy(dtype="float64")

    years = n / periods_per_year
    total = float(np.prod(1.0 + r) - 1.0)
    cagr = _cagr(total, years)

    sd = float(np.std(r, ddof=1)) if n > 1 else 0.0
    ann_vol = sd * np.sqrt(periods_per_year)
    rf_per = (1.0 + risk_free_annual) ** (1.0 / periods_per_year) - 1.0
    excess = r - rf_per
    sharpe = float(np.mean(excess) / sd * np.sqrt(periods_per_year)) if sd > 0 else float("nan")

    downside = excess[excess < 0]
    dsd = float(np.sqrt(np.mean(downside**2))) if downside.size else 0.0
    sortino = float(np.mean(excess) / dsd * np.sqrt(periods_per_year)) if dsd > 0 else float("nan")

    mdd, mdd_days = max_drawdown(eq)
    calmar = float(cagr / abs(mdd)) if mdd < 0 and np.isfinite(cagr) else float("nan")

    gross = 1.0 + r
    geo = float(np.expm1(np.mean(np.log(gross[gross > 0])))) if np.any(gross > 0) else float("nan")

    sk = float(stats.skew(r)) if n > 2 else 0.0
    ku = float(stats.kurtosis(r)) if n > 3 else 0.0
    per_period_sr = float(np.mean(excess) / sd) if sd > 0 else 0.0

    return Metrics(
        n=n,
        years=years,
        total_return=total,
        cagr=cagr,
        ann_vol=ann_vol,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=mdd,
        max_drawdown_days=mdd_days,
        calmar=calmar,
        hit_rate=float(np.mean(r > 0)),
        mean_return=float(np.mean(r)),
        median_return=float(np.median(r)),
        geo_mean_return=geo,
        skew=sk,
        excess_kurtosis=ku,
        best=float(np.max(r)),
        worst=float(np.min(r)),
        var_95=float(np.percentile(r, 5)),
        cvar_95=float(np.mean(r[r <= np.percentile(r, 5)])) if n >= 20 else float("nan"),
        var_99=float(np.percentile(r, 1)),
        cvar_99=float(np.mean(r[r <= np.percentile(r, 1)])) if n >= 100 else float("nan"),
        psr_vs_zero=probabilistic_sharpe(per_period_sr, n, sk, ku, 0.0),
        t_stat=float(np.mean(r) / (sd / np.sqrt(n))) if sd > 0 else float("nan"),
        exposure_fraction=exposure_fraction,
        top10_share_of_logsum=tail_concentration(r, 10),
        extra={
            "n_positive": float(np.sum(r > 0)),
            "n_negative": float(np.sum(r < 0)),
            "n_flat": float(np.sum(r == 0)),
            "top1_share_of_logsum": tail_concentration(r, 1),
            "top25_share_of_logsum": tail_concentration(r, 25),
            "return_per_hour_exposed": (
                float(np.log1p(total) / (n * HOURS_PER_SESSION_OVERNIGHT)) if total > -1 and n else float("nan")
            ),
        },
    )


def compare(results: dict[str, pd.Series], **kw) -> pd.DataFrame:
    """Metric table for several return series side by side."""
    rows = {name: compute(series, **kw).to_dict() for name, series in results.items()}
    return pd.DataFrame(rows).T
