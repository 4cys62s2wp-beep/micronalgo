"""Network-backed providers: Stooq, Yahoo, Tiingo, Alpaca.

None of these can be reached from the build environment (strict egress
allowlist), so they are written to be *verifiable without them*: every parser is
a pure function over a response body, exercised in ``tests/test_providers.py``
against recorded fixture payloads. The only untested surface is the HTTP call
itself, which ``micronalgo preflight`` checks on the user's machine.

Ranking, best first for this specific study:

``stooq``
    Free, no key, full history, and OHLC are **all** adjusted consistently --
    which is exactly what the overnight decomposition needs. Weakness: no
    official SLA, and it silently returns an HTML error page instead of a CSV
    when rate-limited, so the parser checks the body shape rather than trusting
    the status code.
``yahoo``
    Free, full history, but returns split-adjusted OHLC plus a separate
    ``Adj Close``. The ratio ``AdjClose/Close`` must be applied to all four
    columns; using ``Adj Close`` next to a raw ``Open`` is the single most common
    way this study gets silently ruined.
``tiingo`` / ``alpaca``
    Keyed, and both return explicit adjusted *and* raw fields, which is the
    cleanest input available. Alpaca's history begins in 2016, so it cannot
    answer the historical question -- but it is the right source for the live
    leg because it is the venue's own view of the tape.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
from typing import Any

import pandas as pd
import requests

from ..schema import Adjustment, coerce
from .base import Fetched, ProviderError

# (connect, read). A short connect timeout matters: when a provider is simply
# unreachable, the whole fallback chain otherwise stalls for 30 silent seconds
# per provider and the command looks hung.
DEFAULT_TIMEOUT = (5, 30)
USER_AGENT = "micronalgo/1.0 (+research)"


def _get(url: str, *, params: dict | None = None, headers: dict | None = None, timeout=DEFAULT_TIMEOUT):
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    try:
        resp = requests.get(url, params=params, headers=h, timeout=timeout)
    except requests.RequestException as exc:
        raise ProviderError(f"request failed: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(f"HTTP {resp.status_code} from {resp.url.split('?')[0]}: {resp.text[:200]}")
    return resp


# --------------------------------------------------------------------------- #
# Stooq
# --------------------------------------------------------------------------- #

def parse_stooq_csv(body: str) -> pd.DataFrame:
    """Parse a Stooq daily CSV body into the canonical schema.

    Stooq returns ``Date,Open,High,Low,Close,Volume`` fully adjusted. On failure
    it returns an HTML page or the literal text ``No data``, both with HTTP 200 --
    hence the explicit shape check.
    """
    head = body.lstrip()[:200].lower()
    if not head.startswith("date,"):
        raise ProviderError(f"stooq did not return a CSV (rate limited?): {body[:120]!r}")
    df = pd.read_csv(io.StringIO(body))
    df.columns = [c.strip().lower() for c in df.columns]
    need = {"date", "open", "high", "low", "close"}
    if not need.issubset(df.columns):
        raise ProviderError(f"stooq csv missing columns: {sorted(need - set(df.columns))}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    out = pd.DataFrame(index=df.index)
    for c in ("open", "high", "low", "close"):
        out[c] = pd.to_numeric(df[c], errors="coerce")
        out[f"raw_{c}"] = out[c]
    out["volume"] = pd.to_numeric(df.get("volume"), errors="coerce")
    out["adj_factor"] = 1.0
    return coerce(out)


class StooqProvider:
    name = "stooq"
    requires_key = False
    URL = "https://stooq.com/q/d/l/"

    def available(self) -> bool:
        return True

    def fetch(self, symbol: str, start: dt.date | None = None, end: dt.date | None = None) -> Fetched:
        sym = symbol.lower()
        if "." not in sym:
            sym = f"{sym}.us"
        resp = _get(self.URL, params={"s": sym, "i": "d"})
        frame = parse_stooq_csv(resp.text)
        frame = _slice(frame, start, end)
        return Fetched(
            frame,
            Adjustment(True, True, False, "stooq delivers fully adjusted OHLC; as-traded prices unavailable"),
            self.name,
            dt.datetime.now(dt.timezone.utc),
        )


# --------------------------------------------------------------------------- #
# Yahoo
# --------------------------------------------------------------------------- #

def parse_yahoo_chart(payload: dict[str, Any]) -> pd.DataFrame:
    """Parse a Yahoo ``/v8/finance/chart`` JSON payload.

    Applies ``adjclose/close`` uniformly to all four OHLC columns. Yahoo's raw
    OHLC are split-adjusted but not dividend-adjusted, so the resulting
    ``raw_*`` columns are 'as-traded modulo splits' -- recorded in the
    :class:`Adjustment` note rather than pretended away.
    """
    try:
        result = payload["chart"]["result"][0]
        stamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        err = (payload or {}).get("chart", {}).get("error")
        raise ProviderError(f"unexpected yahoo payload (error={err}): {str(payload)[:200]}") from exc

    idx = pd.to_datetime(pd.Series(stamps, dtype="int64"), unit="s", utc=True)
    idx = idx.dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)

    df = pd.DataFrame(
        {
            "raw_open": quote.get("open"),
            "raw_high": quote.get("high"),
            "raw_low": quote.get("low"),
            "raw_close": quote.get("close"),
            "volume": quote.get("volume"),
        },
        index=pd.DatetimeIndex(idx, name="date"),
    )

    adj = result["indicators"].get("adjclose")
    if adj and adj[0].get("adjclose") is not None:
        adjclose = pd.Series(adj[0]["adjclose"], index=df.index, dtype="float64")
        factor = (adjclose / df["raw_close"]).replace([float("inf"), float("-inf")], pd.NA)
        factor = factor.astype("float64").ffill().bfill().fillna(1.0)
    else:
        factor = pd.Series(1.0, index=df.index)

    for c in ("open", "high", "low", "close"):
        df[c] = df[f"raw_{c}"] * factor
    df["adj_factor"] = factor
    return coerce(df.dropna(subset=["close"]))


class YahooProvider:
    name = "yahoo"
    requires_key = False
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def available(self) -> bool:
        return True

    def fetch(self, symbol: str, start: dt.date | None = None, end: dt.date | None = None) -> Fetched:
        params = {
            "period1": int(dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc).timestamp()) if start is None
            else int(dt.datetime.combine(start, dt.time()).replace(tzinfo=dt.timezone.utc).timestamp()),
            "period2": int(dt.datetime.now(dt.timezone.utc).timestamp()) if end is None
            else int(dt.datetime.combine(end, dt.time(23, 59)).replace(tzinfo=dt.timezone.utc).timestamp()),
            "interval": "1d",
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
        resp = _get(self.URL.format(symbol=symbol.upper()), params=params)
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"yahoo returned non-JSON: {resp.text[:200]}") from exc
        frame = _slice(parse_yahoo_chart(payload), start, end)
        return Fetched(
            frame,
            Adjustment(True, True, False, "raw_* are split-adjusted but not dividend-adjusted"),
            self.name,
            dt.datetime.now(dt.timezone.utc),
        )


# --------------------------------------------------------------------------- #
# Tiingo
# --------------------------------------------------------------------------- #

def parse_tiingo(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Parse Tiingo's daily JSON, which helpfully ships adjusted *and* raw fields."""
    if not rows:
        raise ProviderError("tiingo returned no rows")
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        raise ProviderError(f"unexpected tiingo payload keys: {sorted(df.columns)[:10]}")
    idx = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
    out = pd.DataFrame(index=pd.DatetimeIndex(idx, name="date"))
    for c in ("open", "high", "low", "close"):
        out[c] = pd.to_numeric(df.get(f"adj{c.capitalize()}", df.get(c)), errors="coerce").to_numpy()
        out[f"raw_{c}"] = pd.to_numeric(df.get(c), errors="coerce").to_numpy()
    out["volume"] = pd.to_numeric(df.get("adjVolume", df.get("volume")), errors="coerce").to_numpy()
    out["adj_factor"] = (out["close"] / out["raw_close"]).replace([float("inf"), float("-inf")], 1.0)
    return coerce(out)


class TiingoProvider:
    name = "tiingo"
    requires_key = True
    URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"

    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.getenv("TIINGO_API_KEY", "")

    def available(self) -> bool:
        return bool(self.token)

    def fetch(self, symbol: str, start: dt.date | None = None, end: dt.date | None = None) -> Fetched:
        if not self.token:
            raise ProviderError("TIINGO_API_KEY not set")
        params = {
            "startDate": (start or dt.date(1980, 1, 1)).isoformat(),
            "endDate": (end or dt.date.today()).isoformat(),
            "format": "json",
            "resampleFreq": "daily",
        }
        resp = _get(
            self.URL.format(symbol=symbol.lower()),
            params=params,
            headers={"Authorization": f"Token {self.token}", "Content-Type": "application/json"},
        )
        frame = _slice(parse_tiingo(resp.json()), start, end)
        return Fetched(
            frame,
            Adjustment(True, True, True, "tiingo ships adjusted and as-traded fields separately"),
            self.name,
            dt.datetime.now(dt.timezone.utc),
        )


# --------------------------------------------------------------------------- #
# Alpaca market data
# --------------------------------------------------------------------------- #

def parse_alpaca_bars(payload: dict[str, Any], symbol: str) -> pd.DataFrame:
    """Parse Alpaca ``/v2/stocks/bars`` JSON.

    Handles both the multi-symbol (``{"bars": {"MU": [...]}}``) and legacy
    single-symbol (``{"bars": [...]}``) response shapes, because which one you
    get depends on the endpoint variant. [verify-at-runtime]
    """
    bars = payload.get("bars")
    if isinstance(bars, dict):
        bars = bars.get(symbol.upper(), [])
    if not bars:
        raise ProviderError(f"alpaca returned no bars for {symbol}")
    df = pd.DataFrame(bars)
    idx = pd.to_datetime(df["t"], errors="coerce", utc=True).dt.tz_convert("America/New_York")
    idx = idx.dt.normalize().dt.tz_localize(None)
    out = pd.DataFrame(index=pd.DatetimeIndex(idx, name="date"))
    for src, dst in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
        out[dst] = pd.to_numeric(df[src], errors="coerce").to_numpy()
        out[f"raw_{dst}"] = out[dst]
    out["volume"] = pd.to_numeric(df.get("v"), errors="coerce").to_numpy()
    out["adj_factor"] = 1.0
    return coerce(out)


class AlpacaDataProvider:
    name = "alpaca"
    requires_key = True
    URL = "https://data.alpaca.markets/v2/stocks/bars"

    def __init__(self, key: str | None = None, secret: str | None = None, *, feed: str = "iex") -> None:
        self.key = key or os.getenv("ALPACA_API_KEY_ID", "")
        self.secret = secret or os.getenv("ALPACA_API_SECRET_KEY", "")
        self.feed = feed

    def available(self) -> bool:
        return bool(self.key and self.secret)

    def fetch(self, symbol: str, start: dt.date | None = None, end: dt.date | None = None) -> Fetched:
        if not self.available():
            raise ProviderError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set")
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}
        params: dict[str, Any] = {
            "symbols": symbol.upper(),
            "timeframe": "1Day",
            "adjustment": "all",
            "feed": self.feed,
            "limit": 10000,
            "start": (start or dt.date(2016, 1, 1)).isoformat(),
        }
        if end is not None:
            params["end"] = end.isoformat()

        frames: list[pd.DataFrame] = []
        token: str | None = None
        for _ in range(50):  # bounded pagination; 500k bars is far beyond any daily history
            if token:
                params["page_token"] = token
            payload = _get(self.URL, params=params, headers=headers).json()
            frames.append(parse_alpaca_bars(payload, symbol))
            token = payload.get("next_page_token")
            if not token:
                break
        frame = _slice(coerce(pd.concat(frames)), start, end)
        return Fetched(
            frame,
            Adjustment(True, True, False, "adjustment=all; history begins ~2016"),
            self.name,
            dt.datetime.now(dt.timezone.utc),
        )


def _slice(df: pd.DataFrame, start: dt.date | None, end: dt.date | None) -> pd.DataFrame:
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    return df
