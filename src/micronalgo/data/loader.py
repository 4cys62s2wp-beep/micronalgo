"""Provider chain, on-disk cache, and provenance.

Three properties matter more than convenience here:

**Provenance is recorded, not assumed.** Every cached series carries a sidecar
JSON naming the provider, the adjustment it claimed, when it was fetched, and
the validation findings at fetch time. A result you cannot attribute to a
specific file from a specific provider on a specific day is not a result.

**Fallback is loud.** If the preferred provider fails, the next one is tried and
the substitution is reported. Silently swapping the data source underneath a
backtest is how two runs of the same command produce two different histories.

**The cache never hides staleness.** Cached data is served only if it is younger
than ``max_age`` or ``offline`` was requested explicitly; otherwise it is
refreshed. For live trading, freshness is a hard validation check.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from .providers import (
    AlpacaDataProvider,
    CsvProvider,
    Provider,
    ProviderError,
    StooqProvider,
    TiingoProvider,
    YahooProvider,
)
from .schema import coerce, validate_schema
from .validate import Severity, ValidationReport, check_cross_provider, validate

DEFAULT_CACHE = Path("data/cache")
PROVIDER_ORDER = ("stooq", "yahoo", "tiingo", "alpaca")


@dataclass
class Provenance:
    symbol: str
    provider: str
    adjustment: str
    adjustment_note: str
    fetched_at: str
    rows: int
    first: str
    last: str
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    fallback_from: list[str] = field(default_factory=list)

    def render(self) -> str:
        return (
            f"{self.symbol} <- {self.provider} | {self.rows} rows {self.first}..{self.last}\n"
            f"  adjustment : {self.adjustment} ({self.adjustment_note})\n"
            f"  fetched    : {self.fetched_at}\n"
            + (f"  FELL BACK  : tried {', '.join(self.fallback_from)} first\n" if self.fallback_from else "")
            + (f"  warnings   : {len(self.validation_warnings)}\n" if self.validation_warnings else "")
            + (f"  ERRORS     : {len(self.validation_errors)}\n" if self.validation_errors else "")
        )


@dataclass
class LoadedSeries:
    frame: pd.DataFrame
    provenance: Provenance
    report: ValidationReport

    @property
    def ok(self) -> bool:
        return self.report.ok


def build_provider(name: str, **kw) -> Provider:
    name = name.lower()
    if name == "stooq":
        return StooqProvider()
    if name == "yahoo":
        return YahooProvider()
    if name == "tiingo":
        return TiingoProvider(kw.get("tiingo_token"))
    if name == "alpaca":
        return AlpacaDataProvider(kw.get("alpaca_key"), kw.get("alpaca_secret"), feed=kw.get("feed", "iex"))
    if name.startswith("csv:"):
        return CsvProvider(name.split(":", 1)[1])
    raise KeyError(f"unknown provider {name!r}")


def _cache_paths(cache_dir: Path, symbol: str, provider: str) -> tuple[Path, Path]:
    stem = f"{symbol.upper()}_{provider.replace(':', '_').replace('/', '_')}"
    return cache_dir / f"{stem}.parquet", cache_dir / f"{stem}.json"


def write_cache(frame: pd.DataFrame, prov: Provenance, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    pq, meta = _cache_paths(cache_dir, prov.symbol, prov.provider)
    try:
        frame.to_parquet(pq)
    except Exception:
        pq = pq.with_suffix(".csv")
        frame.to_csv(pq)
    meta.write_text(json.dumps(asdict(prov), indent=2))
    return pq


def read_cache(symbol: str, provider: str, cache_dir: Path) -> tuple[pd.DataFrame, Provenance] | None:
    pq, meta = _cache_paths(cache_dir, symbol, provider)
    if not meta.exists():
        return None
    try:
        if pq.exists():
            frame = pd.read_parquet(pq)
        elif pq.with_suffix(".csv").exists():
            frame = pd.read_csv(pq.with_suffix(".csv"), index_col=0, parse_dates=True)
        else:
            return None
        frame = coerce(frame)
        validate_schema(frame)
        prov = Provenance(**json.loads(meta.read_text()))
        return frame, prov
    except Exception:
        return None


def load(
    symbol: str = "MU",
    *,
    providers: tuple[str, ...] | list[str] = PROVIDER_ORDER,
    start: dt.date | None = None,
    end: dt.date | None = None,
    cache_dir: Path | str = DEFAULT_CACHE,
    refresh: bool = False,
    offline: bool = False,
    max_cache_age_hours: float = 18.0,
    check_fresh: bool = False,
    on_progress: Callable[[str], None] | None = None,
    **provider_kw,
) -> LoadedSeries:
    """Load a validated price series, preferring the first provider that works.

    ``offline=True`` uses the cache only and never touches the network -- the
    mode used by CI and by anyone on a locked-down connection.
    """
    cache_dir = Path(cache_dir)
    tried: list[str] = []
    errors: list[str] = []
    say = on_progress or (lambda _msg: None)

    for name in providers:
        cached = read_cache(symbol, name, cache_dir)
        if cached is not None:
            frame, prov = cached
            age = _age_hours(prov.fetched_at)
            fresh_enough = age is not None and age <= max_cache_age_hours
            if offline or (fresh_enough and not refresh):
                say(f"{name}: using cached data ({len(frame):,} rows)")
                sliced = _slice(frame, start, end)
                report = validate(sliced, check_fresh=check_fresh)
                prov.fallback_from = tried.copy()
                return LoadedSeries(sliced, prov, report)
        if offline:
            tried.append(name)
            errors.append(f"{name}: no usable cache and offline=True")
            continue

        try:
            provider = build_provider(name, **provider_kw)
        except KeyError as exc:
            errors.append(str(exc))
            continue
        if not provider.available():
            tried.append(name)
            errors.append(f"{name}: not configured (missing API key?)")
            continue

        say(f"{name}: downloading {symbol} ...")
        try:
            fetched = provider.fetch(symbol, start, end)
        except ProviderError as exc:
            tried.append(name)
            errors.append(f"{name}: {exc}")
            say(f"{name}: unavailable, trying the next provider")
            continue
        say(f"{name}: got {len(fetched.frame):,} rows")

        frame = fetched.frame
        report = validate(frame, check_fresh=check_fresh)
        prov = Provenance(
            symbol=symbol.upper(),
            provider=fetched.provider,
            adjustment=fetched.adjustment.label,
            adjustment_note=fetched.adjustment.note,
            fetched_at=fetched.fetched_at.isoformat(),
            rows=len(frame),
            first=str(frame.index[0].date()) if len(frame) else "",
            last=str(frame.index[-1].date()) if len(frame) else "",
            validation_errors=[str(f) for f in report.errors],
            validation_warnings=[str(f) for f in report.warnings],
            fallback_from=tried.copy(),
        )
        write_cache(frame, prov, cache_dir)
        return LoadedSeries(_slice(frame, start, end), prov, report)

    raise ProviderError(
        "no provider could deliver data for "
        f"{symbol}. Tried {list(providers)}.\n  " + "\n  ".join(errors)
        + "\n\nFallback that always works: export the history to CSV and use\n"
        "  micronalgo fetch --provider csv:/path/to/MU.csv"
    )


def load_two_and_compare(
    symbol: str = "MU",
    *,
    primary: str = "stooq",
    secondary: str = "yahoo",
    **kw,
) -> tuple[LoadedSeries, LoadedSeries | None, ValidationReport]:
    """Load from two providers and cross-check the overnight series.

    This is the strongest available guard against a silently wrong history, and
    the report marks a study that skipped it as unverified.
    """
    a = load(symbol, providers=(primary,), **kw)
    cross = ValidationReport(n_rows=len(a.frame))
    try:
        b = load(symbol, providers=(secondary,), **kw)
    except ProviderError as exc:
        cross.add("cross_provider", Severity.WARN, f"secondary provider unavailable: {exc}")
        return a, None, cross
    check_cross_provider(a.frame, b.frame, cross, names=(primary, secondary))
    return a, b, cross


def _slice(df: pd.DataFrame, start: dt.date | None, end: dt.date | None) -> pd.DataFrame:
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    return df


def _age_hours(iso: str) -> float | None:
    try:
        ts = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600.0
