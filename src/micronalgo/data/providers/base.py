"""Provider interface.

Every provider returns the canonical schema or raises :class:`ProviderError`.
No provider is allowed to return a partially-adjusted frame silently: it must
declare its :class:`~micronalgo.data.schema.Adjustment` so the loader can record
what it got and the validator can check the claim against the data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from ..schema import Adjustment


class ProviderError(RuntimeError):
    """Provider could not deliver usable data."""


@dataclass(frozen=True)
class Fetched:
    frame: pd.DataFrame
    adjustment: Adjustment
    provider: str
    fetched_at: dt.datetime


class Provider(Protocol):
    name: str
    requires_key: bool

    def available(self) -> bool:
        """True if this provider is configured well enough to try."""
        ...

    def fetch(self, symbol: str, start: dt.date | None = None, end: dt.date | None = None) -> Fetched:
        ...
