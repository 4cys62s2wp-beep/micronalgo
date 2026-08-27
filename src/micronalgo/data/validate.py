"""Data validation -- the gate every series must pass before it is believed.

This project's central number is a ratio between two prices recorded a few hours
apart. That makes it exquisitely sensitive to exactly the defects vendors are
worst at, so the validator is not a formality.

What actually detects a mis-adjusted series
-------------------------------------------
A tempting check is the identity ``(1+r_cc) == (1+r_on)*(1+r_id)``. It does not
work for this purpose: derived from one pair of columns, ``O_t`` cancels and the
identity holds no matter how wrong the adjustment is. It is kept as a *wiring*
check only.

The checks that do work:

``check_adjustment``
    ``adj_factor`` changes on exactly the sessions that carry a corporate
    action. If the series was adjusted consistently, the overnight return on
    those sessions is unremarkable. If ``close`` was adjusted and ``open`` was
    not, every ex-date carries an overnight return near ``1/k - 1``. Comparing
    the two populations is a direct, powerful test.
``check_split_artifacts``
    Any overnight or close-to-close move whose ratio sits near a common split
    ratio (1:2, 1:3, 2:3, 1:4, 1:5, 3:2, 2:1 ...) *and* is not corroborated by
    the session's own high/low range is flagged. A real -50 % gap drags the
    day's range with it; a phantom split gap does not.
``check_cross_provider``
    Two independent providers must agree on the overnight return series to
    within a tight tolerance. Disagreement means at least one is wrong, and the
    study cannot proceed on either until it is resolved.

Severity
--------
``ERROR`` blocks use of the series. ``WARN`` is recorded in the report and shown
to the user. ``INFO`` is provenance. Nothing is ever silently swallowed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from ..calendar_nyse import Calendar, CalendarError, research_calendar
from .schema import REQUIRED_COLS, SchemaError, validate_schema


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


@dataclass
class Finding:
    check: str
    severity: Severity
    message: str
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.severity.value:<5}] {self.check}: {self.message}"


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)
    n_rows: int = 0
    first: dt.date | None = None
    last: dt.date | None = None

    def add(self, check: str, severity: Severity, message: str, **detail) -> None:
        self.findings.append(Finding(check, severity, message, detail))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        head = (
            f"rows={self.n_rows} range={self.first}..{self.last} "
            f"errors={len(self.errors)} warnings={len(self.warnings)}"
        )
        return "\n".join([head, *(str(f) for f in self.findings)])

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise SchemaError("data validation failed:\n" + "\n".join(str(f) for f in self.errors))


# Common split ratios expressed as the price multiplier applied on the ex-date.
_SPLIT_MULTIPLIERS = (
    0.5, 1 / 3, 0.25, 0.2, 0.1, 2 / 3, 0.75, 0.4,      # forward splits
    2.0, 3.0, 4.0, 5.0, 10.0, 1.5, 1 / 0.75, 2.5,      # reverse splits
)
_SPLIT_TOL = 0.02


def _near_split(ratio: float, tol: float = _SPLIT_TOL) -> float | None:
    for m in _SPLIT_MULTIPLIERS:
        if abs(ratio - m) <= tol * m:
            return m
    return None


def check_schema(df: pd.DataFrame, report: ValidationReport) -> None:
    try:
        validate_schema(df)
        report.add("schema", Severity.INFO, "canonical schema satisfied")
    except SchemaError as exc:
        report.add("schema", Severity.ERROR, str(exc))


def check_prices(df: pd.DataFrame, report: ValidationReport) -> None:
    """Positivity, NaNs, and OHLC internal consistency."""
    for col in REQUIRED_COLS:
        if col == "volume":
            continue
        s = df[col]
        n_nan = int(s.isna().sum())
        n_bad = int((s <= 0).sum())
        if n_nan:
            sev = Severity.ERROR if n_nan > 0.001 * len(df) else Severity.WARN
            report.add("prices.nan", sev, f"{col}: {n_nan} NaN values", column=col, count=n_nan)
        if n_bad:
            report.add("prices.nonpositive", Severity.ERROR, f"{col}: {n_bad} values <= 0", column=col, count=n_bad)

    for prefix in ("", "raw_"):
        o, h, l_, c = (df[f"{prefix}{x}"] for x in ("open", "high", "low", "close"))
        viol = (h < l_) | (o > h) | (o < l_) | (c > h) | (c < l_)
        n = int(viol.fillna(False).sum())
        if n:
            dates = df.index[viol.fillna(False)].strftime("%Y-%m-%d").tolist()[:5]
            report.add(
                "prices.ohlc_range",
                Severity.ERROR if n > 3 else Severity.WARN,
                f"{prefix or 'adj'}OHLC inconsistent on {n} sessions (open/close outside [low,high])",
                count=n, examples=dates,
            )


def check_frozen_rows(df: pd.DataFrame, report: ValidationReport, *, max_run: int = 5) -> None:
    """Consecutive identical OHLC rows indicate a stalled feed, not a quiet market."""
    key = df[["open", "high", "low", "close"]].round(6)
    same = (key == key.shift(1)).all(axis=1)
    run = longest = 0
    end_idx = None
    for ts, flag in same.items():
        run = run + 1 if flag else 0
        if run > longest:
            longest, end_idx = run, ts
    if longest >= max_run:
        report.add(
            "quality.frozen",
            Severity.WARN,
            f"{longest + 1} consecutive identical OHLC rows ending {end_idx.date() if end_idx is not None else '?'}",
            run=longest + 1,
        )


def check_adjustment(df: pd.DataFrame, report: ValidationReport, *, factor_tol: float = 1e-6) -> None:
    """The real mis-adjustment detector: are ex-date overnight returns anomalous?

    On sessions where ``adj_factor`` steps, a consistently adjusted series shows
    an ordinary overnight return. A series whose ``close`` was adjusted but whose
    ``open`` was not shows a return of roughly ``1/k - 1`` on every one of them.
    """
    factor = df["adj_factor"]
    step = (factor / factor.shift(1) - 1.0).abs()
    ex_dates = df.index[(step > factor_tol).fillna(False)]

    r_on = (df["open"] / df["close"].shift(1) - 1.0)
    ordinary = r_on.drop(index=ex_dates, errors="ignore").abs()
    baseline = float(ordinary.quantile(0.999)) if len(ordinary) > 100 else 0.25

    if len(ex_dates) == 0:
        report.add(
            "adjustment.exdates",
            Severity.INFO,
            "adj_factor is constant: provider delivered a pre-adjusted series "
            "(no independent way to cross-check corporate actions)",
        )
    else:
        suspicious = [ts for ts in ex_dates if np.isfinite(r_on.get(ts, np.nan)) and abs(r_on[ts]) > max(baseline, 0.15)]
        if suspicious:
            report.add(
                "adjustment.exdate_gap",
                Severity.ERROR,
                f"{len(suspicious)}/{len(ex_dates)} corporate-action sessions carry an anomalous overnight "
                f"return (|r_on| > {max(baseline, 0.15):.1%}). This is the signature of OHLC columns adjusted "
                "inconsistently -- the overnight study cannot use this series.",
                examples=[str(t.date()) for t in suspicious[:5]],
                baseline=baseline,
            )
        else:
            report.add(
                "adjustment.exdates",
                Severity.INFO,
                f"{len(ex_dates)} corporate-action sessions, all with ordinary overnight returns",
            )


def check_split_artifacts(df: pd.DataFrame, report: ValidationReport) -> None:
    """Flag price jumps that look like an unapplied split rather than a real move."""
    prev_close = df["close"].shift(1)
    ratio_on = df["open"] / prev_close
    hits = []
    for ts, ratio in ratio_on.items():
        if not np.isfinite(ratio) or abs(ratio - 1.0) < 0.25:
            continue
        m = _near_split(float(ratio))
        if m is None:
            continue
        # A genuine move of this size drags the session range with it; a phantom
        # split gap leaves the *previous* day's range untouched around old prices.
        lo, hi = df.at[ts, "low"], df.at[ts, "high"]
        pc = prev_close.get(ts, np.nan)
        corroborated = np.isfinite(pc) and (lo <= pc <= hi)
        if not corroborated:
            hits.append({"date": str(ts.date()), "ratio": round(float(ratio), 4), "split_multiplier": round(m, 4)})

    if hits:
        report.add(
            "adjustment.split_artifact",
            Severity.ERROR,
            f"{len(hits)} overnight moves match a common split ratio and are not corroborated by the "
            "session range -- the series is very likely un- or partially split-adjusted",
            examples=hits[:5],
        )


def check_sessions(
    df: pd.DataFrame, report: ValidationReport, *, calendar: Calendar | None = None, max_missing_frac: float = 0.02
) -> None:
    """Compare delivered sessions with the exchange calendar."""
    cal = calendar or research_calendar()
    if df.empty:
        report.add("sessions", Severity.ERROR, "empty frame")
        return
    start, end = df.index[0].date(), df.index[-1].date()
    try:
        expected = {s.date for s in cal.sessions_between(start, end)}
    except CalendarError as exc:
        report.add("sessions", Severity.WARN, f"calendar unavailable: {exc}")
        return

    have = {ts.date() for ts in df.index}
    missing = sorted(expected - have)
    extra = sorted(have - expected)
    frac = len(missing) / max(len(expected), 1)

    if missing:
        report.add(
            "sessions.missing",
            Severity.ERROR if frac > max_missing_frac else Severity.WARN,
            f"{len(missing)} of {len(expected)} expected sessions absent ({frac:.2%})",
            examples=[str(d) for d in missing[:5]],
        )
    if extra:
        report.add(
            "sessions.unexpected",
            Severity.WARN,
            f"{len(extra)} bars on dates the exchange calendar says are closed",
            examples=[str(d) for d in extra[:5]],
        )


def check_staleness(df: pd.DataFrame, report: ValidationReport, *, max_age_days: int = 5,
                    today: dt.date | None = None) -> None:
    """Refuse to trade on data that has stopped updating."""
    if df.empty:
        return
    today = today or dt.date.today()
    age = (today - df.index[-1].date()).days
    if age > max_age_days:
        report.add(
            "freshness",
            Severity.ERROR,
            f"most recent bar is {age} days old ({df.index[-1].date()}); refusing to treat as current",
            age_days=age,
        )
    else:
        report.add("freshness", Severity.INFO, f"most recent bar {df.index[-1].date()} ({age}d old)")


def check_cross_provider(
    a: pd.DataFrame, b: pd.DataFrame, report: ValidationReport, *,
    names: tuple[str, str] = ("a", "b"), min_corr: float = 0.99, max_median_abs_diff: float = 0.0015,
) -> None:
    """Two providers must tell the same story about the overnight series."""
    common = a.index.intersection(b.index)
    if len(common) < 50:
        report.add("cross_provider", Severity.WARN, f"only {len(common)} overlapping sessions; cannot compare")
        return
    ra = (a.loc[common, "open"] / a.loc[common, "close"].shift(1) - 1.0)
    rb = (b.loc[common, "open"] / b.loc[common, "close"].shift(1) - 1.0)
    both = pd.concat([ra, rb], axis=1).dropna()
    if len(both) < 50:
        report.add("cross_provider", Severity.WARN, "insufficient overlapping overnight returns")
        return
    corr = float(both.iloc[:, 0].corr(both.iloc[:, 1]))
    med = float((both.iloc[:, 0] - both.iloc[:, 1]).abs().median())
    sev = Severity.INFO if (corr >= min_corr and med <= max_median_abs_diff) else Severity.ERROR
    report.add(
        "cross_provider",
        sev,
        f"{names[0]} vs {names[1]}: overnight-return corr={corr:.5f}, median |diff|={med * 1e4:.2f} bps "
        f"over {len(both)} sessions",
        corr=corr, median_abs_diff=med, n=len(both),
    )


def validate(
    df: pd.DataFrame,
    *,
    calendar: Calendar | None = None,
    check_fresh: bool = False,
    today: dt.date | None = None,
) -> ValidationReport:
    """Run the full battery. ``check_fresh`` only matters for live use."""
    report = ValidationReport(
        n_rows=len(df),
        first=df.index[0].date() if len(df) else None,
        last=df.index[-1].date() if len(df) else None,
    )
    check_schema(df, report)
    if report.errors:
        return report
    check_prices(df, report)
    check_adjustment(df, report)
    check_split_artifacts(df, report)
    check_sessions(df, report, calendar=calendar)
    check_frozen_rows(df, report)
    if check_fresh:
        check_staleness(df, report, today=today)
    return report
