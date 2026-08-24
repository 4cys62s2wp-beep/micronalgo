"""Canonical bar schema shared by every data provider.

Why two price sets?
------------------
The overnight/intraday decomposition is *pathologically* sensitive to price
adjustment. A single 2-for-1 split that is applied to ``close`` but not to
``open`` manufactures a fake -50 % overnight return on the ex-date and a fake
+100 % intraday return on the same day. Both legs of the study would then be
garbage while still summing to a plausible close-to-close series.

We therefore always carry *both*:

``open/high/low/close``
    Back-adjusted **total-return** prices. Every OHLC value of a given session
    is multiplied by the *same* per-session factor, which is the only way the
    identity ``(1+r_cc) == (1+r_on)*(1+r_id)`` survives adjustment. These are
    the prices used for **return** computation.

``raw_open/raw_high/raw_low/raw_close``
    As-traded prices. These are the prices used for **share counts**,
    **per-share fees** (FINRA TAF), **notional-based fees** (SEC Section 31)
    and **price-sanity guards**. Using adjusted prices for share counts silently
    misprices every fee in the early part of a long history.

``adj_factor``
    ``adjusted = raw * adj_factor``. Monotonically non-decreasing towards 1.0 at
    the right edge for a back-adjusted series with no future corporate actions.

Index
-----
``DatetimeIndex`` named ``date``, timezone-naive, normalised to midnight. It is
the **exchange session date** (America/New_York), never a UTC timestamp. Storing
a tz-aware UTC timestamp is the classic way to shift half the history by one day.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

INDEX_NAME = "date"

ADJ_COLS = ("open", "high", "low", "close")
RAW_COLS = ("raw_open", "raw_high", "raw_low", "raw_close")
PRICE_COLS = ADJ_COLS + RAW_COLS
REQUIRED_COLS = (*PRICE_COLS, "volume", "adj_factor")

DTYPES: dict[str, str] = dict.fromkeys(REQUIRED_COLS, "float64")


class SchemaError(ValueError):
    """Raised when a frame does not satisfy the canonical bar schema."""


@dataclass(frozen=True)
class Adjustment:
    """Describes how faithfully a provider adjusted the series."""

    splits: bool
    dividends: bool
    raw_is_true_raw: bool
    note: str = ""

    @property
    def label(self) -> str:
        parts = []
        parts.append("split-adj" if self.splits else "SPLIT-UNADJUSTED")
        parts.append("div-adj" if self.dividends else "div-unadjusted")
        parts.append("true-raw" if self.raw_is_true_raw else "raw~=split-adj")
        return ", ".join(parts)


def empty_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], name=INDEX_NAME)
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in REQUIRED_COLS}, index=idx)


def coerce(df: pd.DataFrame, *, sort: bool = True, drop_dupes: bool = True) -> pd.DataFrame:
    """Coerce an arbitrary provider frame into the canonical schema.

    Missing ``raw_*`` columns are filled from the adjusted columns and
    ``adj_factor`` defaults to 1.0 (provider delivered a pre-adjusted series and
    gave us no way to recover as-traded prices).
    """
    out = df.copy()

    if out.index.name != INDEX_NAME:
        if INDEX_NAME in out.columns:
            out = out.set_index(INDEX_NAME)
        else:
            out.index.name = INDEX_NAME

    idx = pd.to_datetime(out.index, errors="coerce")
    if getattr(idx, "tz", None) is not None:
        # A tz-aware index would silently roll dates across midnight.
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    out.index = pd.DatetimeIndex(idx).normalize()
    out.index.name = INDEX_NAME
    out = out[~out.index.isna()]

    for adj_c, raw_c in zip(ADJ_COLS, RAW_COLS, strict=True):
        if adj_c not in out.columns:
            raise SchemaError(f"missing required column {adj_c!r}")
        if raw_c not in out.columns:
            out[raw_c] = out[adj_c]

    if "volume" not in out.columns:
        out["volume"] = np.nan
    if "adj_factor" not in out.columns:
        with np.errstate(divide="ignore", invalid="ignore"):
            out["adj_factor"] = np.where(
                out["raw_close"].to_numpy() > 0,
                out["close"].to_numpy() / out["raw_close"].to_numpy(),
                1.0,
            )

    for col in REQUIRED_COLS:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")

    keep = [*REQUIRED_COLS] + [c for c in out.columns if c not in REQUIRED_COLS]
    out = out[keep]

    if drop_dupes:
        out = out[~out.index.duplicated(keep="last")]
    if sort:
        out = out.sort_index()
    return out


def validate_schema(df: pd.DataFrame) -> None:
    """Raise :class:`SchemaError` unless ``df`` is a well-formed canonical frame."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise SchemaError("index must be a DatetimeIndex")
    if getattr(df.index, "tz", None) is not None:
        raise SchemaError("index must be timezone-naive exchange session dates")
    if df.index.name != INDEX_NAME:
        raise SchemaError(f"index must be named {INDEX_NAME!r}")
    if not df.index.is_monotonic_increasing:
        raise SchemaError("index must be sorted ascending")
    if df.index.has_duplicates:
        dupes = df.index[df.index.duplicated()].strftime("%Y-%m-%d").tolist()[:5]
        raise SchemaError(f"index has duplicate sessions: {dupes}")
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise SchemaError(f"missing required columns: {missing}")
    for col in REQUIRED_COLS:
        if df[col].dtype != np.float64:
            raise SchemaError(f"column {col!r} must be float64, got {df[col].dtype}")
