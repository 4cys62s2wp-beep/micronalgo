"""Local CSV provider -- the fallback that always works.

Point it at any CSV with ``Date,Open,High,Low,Close[,Adj Close][,Volume]``
columns (case-insensitive). This is the escape hatch for a locked-down network,
a paid vendor export, or a manually curated file, and it is the provider used in
CI where no host is reachable.

If an ``Adj Close`` column is present the frame is treated as Yahoo-style:
``adj_factor = AdjClose / Close`` is applied uniformly to all four OHLC values,
which is the only adjustment that preserves the overnight/intraday identity.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from ..schema import Adjustment, coerce
from .base import Fetched, ProviderError

_ALIASES = {
    "date": "date", "timestamp": "date", "time": "date", "datum": "date",
    "open": "open", "high": "high", "low": "low", "close": "close",
    "adj close": "adj_close", "adjclose": "adj_close", "adj_close": "adj_close",
    "adjusted close": "adj_close", "volume": "volume", "vol": "volume",
}


class CsvProvider:
    name = "csv"
    requires_key = False

    def __init__(self, path: str | Path, *, already_adjusted: bool = False) -> None:
        self.path = Path(path)
        self.already_adjusted = already_adjusted

    def available(self) -> bool:
        return self.path.exists()

    def fetch(self, symbol: str, start: dt.date | None = None, end: dt.date | None = None) -> Fetched:
        if not self.path.exists():
            raise ProviderError(f"csv not found: {self.path}")
        raw = pd.read_csv(self.path)
        raw.columns = [_ALIASES.get(str(c).strip().lower(), str(c).strip().lower()) for c in raw.columns]
        missing = {"date", "open", "high", "low", "close"} - set(raw.columns)
        if missing:
            raise ProviderError(f"{self.path.name}: missing columns {sorted(missing)}")

        raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
        raw = raw.dropna(subset=["date"]).set_index("date").sort_index()

        for c in ("open", "high", "low", "close", "adj_close", "volume"):
            if c in raw.columns:
                raw[c] = pd.to_numeric(raw[c], errors="coerce")

        out = pd.DataFrame(index=raw.index)
        for c in ("open", "high", "low", "close"):
            out[f"raw_{c}"] = raw[c]

        if "adj_close" in raw.columns and raw["adj_close"].notna().any():
            factor = (raw["adj_close"] / raw["close"]).replace([float("inf"), float("-inf")], pd.NA)
            factor = factor.astype("float64").ffill().bfill().fillna(1.0)
            adjustment = Adjustment(True, True, True, "Adj Close ratio applied uniformly to OHLC")
        else:
            factor = pd.Series(1.0, index=raw.index)
            adjustment = Adjustment(
                self.already_adjusted, self.already_adjusted, not self.already_adjusted,
                "no Adj Close column; adjustment status taken from --already-adjusted flag",
            )

        for c in ("open", "high", "low", "close"):
            out[c] = raw[c] * factor
        out["adj_factor"] = factor
        out["volume"] = raw["volume"] if "volume" in raw.columns else float("nan")

        frame = coerce(out)
        if start is not None:
            frame = frame[frame.index >= pd.Timestamp(start)]
        if end is not None:
            frame = frame[frame.index <= pd.Timestamp(end)]
        if frame.empty:
            raise ProviderError(f"{self.path.name}: no rows in requested range")
        return Fetched(frame, adjustment, f"csv:{self.path.name}", dt.datetime.now(dt.timezone.utc))
