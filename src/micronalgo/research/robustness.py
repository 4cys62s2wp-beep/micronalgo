"""Robustness testing: is the edge real, or is it a story about 30 lucky days?

Why not just a t-test
---------------------
A t-test on daily overnight returns assumes i.i.d. normal observations. MU's
overnight returns are neither: they are heavy-tailed (earnings gaps), mildly
autocorrelated, and heteroskedastic in obvious regimes. A t-stat of 3 on such a
series routinely corresponds to a p-value far worse than 0.001.

So this module uses:

* **Stationary bootstrap** (Politis & Romano 1994) -- resamples geometrically
  distributed *blocks*, preserving short-range dependence while randomising the
  sequence. Confidence intervals from it are honest about serial structure.
* **Circular block permutation** -- a null in which the returns exist but their
  *timing* is scrambled, testing whether "overnight" specifically matters.
* **Sign-flip test** -- a null of a symmetric zero-mean distribution, which
  needs no distributional assumption at all.
* **Subperiod decomposition** -- the single most informative test for this
  anomaly. If the entire effect lives before decimalisation (April 2001), it is
  a microstructure artefact and not a tradable premium.
* **Drop-the-best-N** -- how much of the result survives deleting the largest
  contributors.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .metrics import TRADING_DAYS

# Structural breakpoints that matter for this specific anomaly.
REGIME_BREAKS: tuple[tuple[str, dt.date], ...] = (
    ("sixteenths", dt.date(1997, 6, 24)),      # tick size 1/8 -> 1/16
    ("decimalisation", dt.date(2001, 4, 9)),   # 1/16 -> $0.01; spread collapse
    ("reg-nms", dt.date(2007, 7, 9)),          # Reg NMS full compliance
    ("post-gfc", dt.date(2010, 1, 1)),         # HFT maturity, ETF growth
    ("modern", dt.date(2016, 1, 1)),           # widely-published anomaly
    ("post-covid", dt.date(2020, 4, 1)),       # retail flow regime
)


@dataclass
class BootstrapResult:
    statistic: str
    observed: float
    mean: float
    std: float
    ci_low: float
    ci_high: float
    p_value_gt_zero: float
    n_resamples: int
    block_length: float
    samples: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))

    def significant(self, alpha: float = 0.05) -> bool:
        return self.p_value_gt_zero < alpha


def auto_block_length(returns: np.ndarray) -> float:
    """Average block length for the stationary bootstrap.

    Uses the ``n**(1/3)`` rule of thumb inflated by observed first-order
    autocorrelation. This is a defensible heuristic, not the full Politis-White
    optimum; :func:`stationary_bootstrap` accepts an explicit override and the
    report shows the value used so a reader can vary it.
    """
    r = returns[np.isfinite(returns)]
    n = r.size
    if n < 20:
        return 1.0
    base = n ** (1.0 / 3.0)
    centred = r - r.mean()
    denom = float(np.dot(centred, centred))
    rho = float(np.dot(centred[:-1], centred[1:]) / denom) if denom > 0 else 0.0
    rho = min(max(rho, 0.0), 0.95)
    return float(max(1.0, base * (1.0 + 2.0 * rho)))


def _stationary_indices(n: int, block_length: float, rng: np.random.Generator) -> np.ndarray:
    """Indices for one stationary-bootstrap resample of length ``n``."""
    p = 1.0 / max(block_length, 1.0)
    idx = np.empty(n, dtype=np.int64)
    i = rng.integers(0, n)
    for k in range(n):
        idx[k] = i
        if rng.random() < p:
            i = int(rng.integers(0, n))
        else:
            i = (i + 1) % n
    return idx


def _statistic(r: np.ndarray, name: str, periods_per_year: int) -> float:
    if r.size == 0:
        return float("nan")
    if name == "mean":
        return float(np.mean(r))
    if name == "geometric_mean":
        g = 1.0 + r
        g = g[g > 0]
        return float(np.expm1(np.mean(np.log(g)))) if g.size else float("nan")
    if name == "sharpe":
        sd = float(np.std(r, ddof=1))
        return float(np.mean(r) / sd * np.sqrt(periods_per_year)) if sd > 0 else float("nan")
    if name == "total_return":
        g = 1.0 + r
        return float(np.prod(g) - 1.0) if np.any(g <= 0) else float(np.expm1(np.sum(np.log(g))))
    raise KeyError(f"unknown statistic {name!r}")


def stationary_bootstrap(
    returns: pd.Series | np.ndarray,
    *,
    statistic: str = "mean",
    n_resamples: int = 2000,
    block_length: float | None = None,
    alpha: float = 0.05,
    seed: int = 20240824,
    periods_per_year: int = TRADING_DAYS,
) -> BootstrapResult:
    """Block-bootstrap confidence interval for a statistic of ``returns``."""
    r = np.asarray(returns, dtype="float64")
    r = r[np.isfinite(r)]
    n = r.size
    if n < 30:
        raise ValueError(f"need at least 30 observations, got {n}")
    b = block_length if block_length is not None else auto_block_length(r)
    rng = np.random.default_rng(seed)

    observed = _statistic(r, statistic, periods_per_year)
    samples = np.empty(n_resamples, dtype="float64")
    for k in range(n_resamples):
        samples[k] = _statistic(r[_stationary_indices(n, b, rng)], statistic, periods_per_year)

    finite = samples[np.isfinite(samples)]
    lo, hi = np.percentile(finite, [100 * alpha / 2, 100 * (1 - alpha / 2)]) if finite.size else (np.nan, np.nan)
    # One-sided bootstrap p-value for H0: statistic <= 0, centred on the observed
    # value so it is a genuine hypothesis test rather than a coverage statement.
    centred = finite - observed
    p = float(np.mean(centred >= observed)) if finite.size else float("nan")
    return BootstrapResult(
        statistic=statistic,
        observed=observed,
        mean=float(np.mean(finite)) if finite.size else float("nan"),
        std=float(np.std(finite, ddof=1)) if finite.size > 1 else float("nan"),
        ci_low=float(lo),
        ci_high=float(hi),
        p_value_gt_zero=p,
        n_resamples=n_resamples,
        block_length=b,
        samples=finite,
    )


def sign_flip_test(
    returns: pd.Series | np.ndarray, *, n_resamples: int = 5000, seed: int = 7, statistic: str = "mean"
) -> dict[str, float]:
    """Randomisation test under a symmetric zero-mean null.

    Assumption-free about the shape of the distribution; only symmetry. Because
    it destroys the sign but keeps the magnitudes, it directly answers "could a
    series with these magnitudes produce this mean by chance?".
    """
    r = np.asarray(returns, dtype="float64")
    r = r[np.isfinite(r)]
    rng = np.random.default_rng(seed)
    observed = _statistic(r, statistic, TRADING_DAYS)
    null = np.array(
        [_statistic(r * rng.choice([-1.0, 1.0], size=r.size), statistic, TRADING_DAYS) for _ in range(n_resamples)]
    )
    null = null[np.isfinite(null)]
    return {
        "observed": observed,
        "null_mean": float(np.mean(null)),
        "null_std": float(np.std(null, ddof=1)),
        "p_value": float((np.sum(null >= observed) + 1) / (null.size + 1)),
        "n_resamples": int(null.size),
    }


def circular_permutation_test(
    r_on: pd.Series, r_id: pd.Series, *, n_resamples: int = 5000, seed: int = 11, block: int = 21
) -> dict[str, float]:
    """Does *overnight* specifically matter, or is any window of this length good?

    Null: the overnight and intraday labels are exchangeable. Under it, the
    difference in mean between the two legs should be centred on zero. Blocks
    are rotated circularly so that volatility clustering survives the shuffle.
    """
    a = np.asarray(r_on, dtype="float64")
    b = np.asarray(r_id, dtype="float64")
    mask = np.isfinite(a) & np.isfinite(b)
    a, b = a[mask], b[mask]
    n = a.size
    observed = float(np.mean(a) - np.mean(b))

    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    null = np.empty(n_resamples, dtype="float64")
    for k in range(n_resamples):
        swap = np.repeat(rng.random(n_blocks) < 0.5, block)[:n]
        pa = np.where(swap, b, a)
        pb = np.where(swap, a, b)
        null[k] = float(np.mean(pa) - np.mean(pb))
    return {
        "observed_diff": observed,
        "null_mean": float(np.mean(null)),
        "null_std": float(np.std(null, ddof=1)),
        "p_value": float((np.sum(np.abs(null) >= abs(observed)) + 1) / (n_resamples + 1)),
        "block": block,
    }


def roll_spread(close: pd.Series) -> dict[str, float]:
    """Roll (1984) effective-spread estimate from serial covariance of returns.

    Observed trades bouncing between bid and ask induce negative first-order
    autocovariance in returns, so ``s = 2*sqrt(-Cov(r_t, r_{t-1}))``.

    **Known blind spot, and the reason it is not the primary estimator here:**
    Roll's estimator run on daily *closes* cannot see a systematic
    close-on-the-bid / open-on-the-ask asymmetry at all. If every close prints on
    the same side of the quote, the close-to-close series contains no bounce and
    the estimator correctly returns ~0 -- while the overnight return is inflated
    by a full spread. :func:`corwin_schultz_spread` is used instead because it
    reads the high/low range and does not depend on which side closes print on.
    """
    r = close.pct_change().dropna().to_numpy(dtype="float64")
    if r.size < 30:
        return {"effective_spread": float("nan"), "autocovariance": float("nan"), "n": float(r.size)}
    cov = float(np.cov(r[:-1], r[1:], ddof=1)[0, 1])
    spread = 2.0 * float(np.sqrt(-cov)) if cov < 0 else float("nan")
    return {"effective_spread": spread, "autocovariance": cov, "n": float(r.size)}


_CS_K = 3.0 - 2.0 * np.sqrt(2.0)


def corwin_schultz_spread(bars: pd.DataFrame) -> dict[str, float]:
    """Corwin & Schultz (2012) high-low proportional effective spread.

    Uses only daily highs and lows. The idea: over a two-day window the observed
    high is nearly always an ask and the observed low nearly always a bid, so the
    high-low range contains both the true variance and one spread. Comparing the
    sum of two single-day ranges with the two-day range separates them, because
    variance scales with time and the spread does not.

        beta  = E[ (ln(H_t/L_t))^2 + (ln(H_t+1/L_t+1))^2 ]
        gamma = (ln(H_[t,t+1] / L_[t,t+1]))^2
        alpha = (sqrt(2*beta) - sqrt(beta)) / (3 - 2*sqrt(2)) - sqrt(gamma / (3 - 2*sqrt(2)))
        S     = 2 * (exp(alpha) - 1) / (1 + exp(alpha))

    Negative daily estimates are set to zero before averaging, which is the
    authors' own recommendation -- they arise from the estimator's noise, not
    from negative spreads.
    """
    need = {"high", "low"}
    if not need.issubset(bars.columns) or len(bars) < 30:
        return {"spread": float("nan"), "n": float(len(bars)), "negative_share": float("nan")}

    h = bars["high"].to_numpy(dtype="float64")
    l_ = bars["low"].to_numpy(dtype="float64")
    ok = np.isfinite(h) & np.isfinite(l_) & (h > 0) & (l_ > 0) & (h >= l_)
    h, l_ = np.where(ok, h, np.nan), np.where(ok, l_, np.nan)

    with np.errstate(divide="ignore", invalid="ignore"):
        log_hl = np.log(h / l_)
        beta = log_hl[:-1] ** 2 + log_hl[1:] ** 2
        h2 = np.maximum(h[:-1], h[1:])
        l2 = np.minimum(l_[:-1], l_[1:])
        gamma = np.log(h2 / l2) ** 2
        alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / _CS_K - np.sqrt(gamma / _CS_K)
        spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))

    finite = np.isfinite(spread)
    if not finite.any():
        return {"spread": float("nan"), "n": 0.0, "negative_share": float("nan")}
    vals = spread[finite]
    negative_share = float(np.mean(vals < 0))
    return {
        "spread": float(np.mean(np.maximum(vals, 0.0))),
        "n": float(vals.size),
        "negative_share": negative_share,
    }


def bid_ask_bounce_check(bars: pd.DataFrame, r_on: pd.Series) -> dict[str, float]:
    """Could the overnight 'premium' just be which side of the quote prints where?

    The failure mode is mechanical and needs no economics at all: if closing
    prints tend to land on the bid and opening prints on the ask, then

        r_on = (1 + s/2)/(1 - s/2) - 1  ~=  s

    appears every single session, with an equal and opposite drag on the intraday
    leg. A full bid-to-ask transition contributes a **whole** effective spread to
    the overnight return -- not half of one -- so that is what the edge is
    compared against.

    Honest limitation, stated because it matters: daily OHLC carries no quotes,
    so this cannot *prove* either explanation. What it can do is say whether the
    artefact is large enough to account for the measured edge. Ruling it out
    properly needs quote or minute-bar data. The era decomposition is the other
    half of the answer: the artefact scales with the tick size, so an edge that
    survives after decimalisation is much harder to explain away.
    """
    cs = corwin_schultz_spread(bars)
    roll = roll_spread(bars["close"])
    mean_on = float(r_on.dropna().mean())
    spread = cs["spread"]
    ratio = mean_on / spread if (np.isfinite(spread) and spread > 0) else float("nan")
    return {
        "mean_overnight": mean_on,
        "corwin_schultz_spread": spread,
        "roll_spread": roll["effective_spread"],
        "edge_over_spread": ratio,
        "within_spread": bool(np.isfinite(ratio) and ratio <= 1.0),
        "cs_negative_share": cs["negative_share"],
    }


def spread_by_era(bars: pd.DataFrame) -> pd.DataFrame:
    """Corwin-Schultz spread per regime.

    The pre-2001 rows are the point: a 1/8 tick on a $10 stock is a 125 bps
    spread, which dwarfs any plausible daily edge and makes the early history
    nearly uninterpretable for this question.
    """
    edges = [pd.Timestamp(d) for _, d in REGIME_BREAKS]
    labels = ["pre-" + REGIME_BREAKS[0][0]] + [name for name, _ in REGIME_BREAKS]
    bins = [pd.Timestamp.min] + edges + [pd.Timestamp.max]
    cut = pd.cut(pd.Series(bars.index, index=bars.index), bins=bins, labels=labels, right=False)

    rows = []
    for key, chunk in bars.groupby(cut, observed=True):
        cs = corwin_schultz_spread(chunk)
        r_on = (chunk["open"] / chunk["close"].shift(1) - 1.0).dropna()
        rows.append({
            "period": str(key),
            "n": int(cs["n"]),
            "spread_bps": cs["spread"] * 1e4,
            "mean_overnight_bps": float(r_on.mean()) * 1e4 if len(r_on) else np.nan,
            "edge_over_spread": (float(r_on.mean()) / cs["spread"]) if cs["spread"] > 0 and len(r_on) else np.nan,
        })
    return pd.DataFrame(rows).set_index("period")


def subperiods(
    returns: pd.Series, *, by: str = "regime", periods_per_year: int = TRADING_DAYS
) -> pd.DataFrame:
    """Statistics per subperiod. ``by`` is ``'regime'``, ``'decade'`` or ``'year'``."""
    r = returns.dropna()
    if r.empty:
        return pd.DataFrame()

    if by == "year":
        groups = r.groupby(r.index.year)
    elif by == "decade":
        groups = r.groupby((r.index.year // 10) * 10)
    elif by == "regime":
        edges = [pd.Timestamp(d) for _, d in REGIME_BREAKS]
        labels = ["pre-" + REGIME_BREAKS[0][0]] + [name for name, _ in REGIME_BREAKS]
        bins = [pd.Timestamp.min] + edges + [pd.Timestamp.max]
        cut = pd.cut(pd.Series(r.index, index=r.index), bins=bins, labels=labels, right=False)
        groups = r.groupby(cut, observed=True)
    else:
        raise KeyError(f"unknown grouping {by!r}")

    rows = []
    for key, chunk in groups:
        c = chunk.to_numpy()
        if c.size == 0:
            continue
        gross = 1.0 + c
        total = float(np.prod(gross) - 1.0) if np.any(gross <= 0) else float(np.expm1(np.sum(np.log(gross))))
        sd = float(np.std(c, ddof=1)) if c.size > 1 else np.nan
        years = c.size / periods_per_year
        rows.append(
            {
                "period": str(key),
                "start": chunk.index[0].date(),
                "end": chunk.index[-1].date(),
                "n": c.size,
                "total_return": total,
                "cagr": (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 and (1 + total) > 0 else np.nan,
                "mean_bps": float(np.mean(c)) * 1e4,
                "sharpe": float(np.mean(c) / sd * np.sqrt(periods_per_year)) if sd and sd > 0 else np.nan,
                "hit_rate": float(np.mean(c > 0)),
                "worst": float(np.min(c)),
            }
        )
    return pd.DataFrame(rows).set_index("period")


def drop_best_days(returns: pd.Series, ks: tuple[int, ...] = (0, 1, 5, 10, 25, 50)) -> pd.DataFrame:
    """Total return after deleting the ``k`` best sessions.

    If dropping 10 of ~7,800 sessions turns a fortune into a loss, the "edge" is
    a handful of gaps and cannot be relied on going forward.
    """
    r = returns.dropna().to_numpy()
    order = np.argsort(r)
    rows = []
    for k in ks:
        keep = np.ones(r.size, dtype=bool)
        if k > 0:
            keep[order[-k:]] = False
        sub = r[keep]
        gross = 1.0 + sub
        total = float(np.prod(gross) - 1.0) if np.any(gross <= 0) else float(np.expm1(np.sum(np.log(gross))))
        rows.append({"dropped": k, "n": int(sub.size), "total_return": total})
    return pd.DataFrame(rows).set_index("dropped")


def day_of_week(returns: pd.Series) -> pd.DataFrame:
    """Mean overnight return by weekday of the *arrival* session.

    Monday's overnight spans the whole weekend. If the premium were compensation
    for calendar-time risk, Monday should be ~3x the others. It usually is not.
    """
    r = returns.dropna()
    names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    g = r.groupby(r.index.dayofweek)
    out = pd.DataFrame(
        {
            "n": g.size(),
            "mean_bps": g.mean() * 1e4,
            "median_bps": g.median() * 1e4,
            "std_bps": g.std() * 1e4,
            "hit_rate": g.apply(lambda x: float((x > 0).mean())),
        }
    )
    out.index = [names.get(i, str(i)) for i in out.index]
    return out


def rolling_stats(returns: pd.Series, window: int = 252) -> pd.DataFrame:
    """Rolling annualised mean/vol/Sharpe -- the picture that shows decay."""
    r = returns.dropna()
    mean = r.rolling(window).mean() * TRADING_DAYS
    vol = r.rolling(window).std() * np.sqrt(TRADING_DAYS)
    return pd.DataFrame({"ann_mean": mean, "ann_vol": vol, "sharpe": mean / vol}).dropna()


def monte_carlo_paths(
    returns: pd.Series,
    *,
    n_paths: int = 1000,
    horizon: int | None = None,
    block_length: float | None = None,
    seed: int = 99,
) -> pd.DataFrame:
    """Distribution of outcomes if the future resembles a reshuffling of the past.

    Reports the quantiles of terminal wealth and of maximum drawdown. The
    drawdown distribution matters more than the wealth distribution: it is what
    determines whether a human can actually stay in the trade.
    """
    r = np.asarray(returns.dropna(), dtype="float64")
    n = horizon or r.size
    b = block_length if block_length is not None else auto_block_length(r)
    rng = np.random.default_rng(seed)

    terminal = np.empty(n_paths)
    max_dd = np.empty(n_paths)
    for k in range(n_paths):
        idx = _stationary_indices(n, b, rng)
        path = np.cumprod(1.0 + r[idx % r.size])
        terminal[k] = path[-1] - 1.0
        peak = np.maximum.accumulate(path)
        max_dd[k] = float(np.min(np.where(peak > 0, path / peak - 1.0, 0.0)))

    qs = [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
    return pd.DataFrame(
        {
            "quantile": qs,
            "terminal_return": np.quantile(terminal, qs),
            "max_drawdown": np.quantile(max_dd, qs),
        }
    ).set_index("quantile")
