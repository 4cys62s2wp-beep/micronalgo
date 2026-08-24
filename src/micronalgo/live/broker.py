"""Broker abstraction.

One ``Protocol``, two implementations: a REST adapter for Alpaca and an
in-process simulator. Everything above this layer -- the state machine, risk
guards, reconciliation -- is written against the Protocol and is therefore
fully testable without a network, which matters because the build environment
has none.

A note on the pattern-day-trader rule
-------------------------------------
A "day trade" is an open and close of the same security on the *same* session.
The overnight strategy buys at the close of D and sells at the open of D+1, so
it is **not** day trading and the PDT rule does not apply, however many round
trips per year it makes. The ``short_intraday`` leg *would* be day trading, and
under $25k of equity FINRA caps that at three per rolling five sessions -- which
alone makes the "short the intraday leg too" idea impractical for most retail
accounts. [verify-at-runtime with your own broker]
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    DAY = "day"
    OPG = "opg"   # participates in the opening auction
    CLS = "cls"   # participates in the closing auction
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, Enum):
    NEW = "new"
    ACCEPTED = "accepted"
    PENDING_NEW = "pending_new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    DONE_FOR_DAY = "done_for_day"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED,
            OrderStatus.REJECTED, OrderStatus.DONE_FOR_DAY,
        }

    @property
    def is_open(self) -> bool:
        return not self.is_terminal

    @classmethod
    def parse(cls, value: str | None) -> OrderStatus:
        try:
            return cls(str(value).lower())
        except ValueError:
            return cls.UNKNOWN


@dataclass
class Order:
    client_order_id: str
    symbol: str
    side: OrderSide
    qty: float
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: float | None = None
    broker_order_id: str = ""
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    submitted_at: dt.datetime | None = None
    filled_at: dt.datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.FILLED and self.filled_qty > 0

    def summary(self) -> str:
        px = f"@{self.filled_avg_price:.4f}" if self.filled_avg_price else ""
        return (
            f"{self.side.value.upper()} {self.qty:g} {self.symbol} "
            f"{self.order_type.value}/{self.time_in_force.value} -> {self.status.value} "
            f"filled={self.filled_qty:g}{px} [{self.client_order_id}]"
        )


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float = 0.0
    market_value: float = 0.0
    unrealized_pl: float = 0.0

    @property
    def is_flat(self) -> bool:
        return abs(self.qty) < 1e-9


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    last_equity: float = 0.0
    currency: str = "USD"
    trading_blocked: bool = False
    account_blocked: bool = False
    pattern_day_trader: bool = False
    daytrade_count: int = 0
    multiplier: float = 1.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.trading_blocked or self.account_blocked


@dataclass
class Clock:
    timestamp: dt.datetime
    is_open: bool
    next_open: dt.datetime | None = None
    next_close: dt.datetime | None = None


class BrokerError(RuntimeError):
    """Broker call failed."""


class TransientBrokerError(BrokerError):
    """Retryable: timeout, 5xx, rate limit. The order state is *unknown*."""


class PermanentBrokerError(BrokerError):
    """Not retryable: bad request, insufficient funds, auth failure."""


class DuplicateOrderError(PermanentBrokerError):
    """The broker rejected the order because its client id already exists.

    This is not a failure -- it is idempotency working. The caller should fetch
    the existing order rather than resubmit.
    """


class Broker(Protocol):
    name: str

    def get_account(self) -> Account: ...
    def get_clock(self) -> Clock: ...
    def get_calendar(self, start: dt.date, end: dt.date) -> dict[dt.date, tuple[dt.time, dt.time]]: ...
    def get_position(self, symbol: str) -> Position | None: ...
    def list_open_orders(self, symbol: str | None = None) -> list[Order]: ...
    def get_order_by_client_id(self, client_order_id: str) -> Order | None: ...
    def submit_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        qty: float,
        order_type: OrderType = OrderType.MARKET,
        time_in_force: TimeInForce = TimeInForce.DAY,
        limit_price: float | None = None,
        client_order_id: str,
        extended_hours: bool = False,
    ) -> Order: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def get_last_price(self, symbol: str) -> float: ...
    def is_tradable(self, symbol: str) -> bool: ...


def make_client_order_id(prefix: str, symbol: str, trade_date: dt.date, leg: str, attempt: int = 0) -> str:
    """Deterministic, collision-free order identity.

    The whole restart-safety story rests on this. Both legs of one overnight
    trade are keyed to the **entry session date**, so a trade has a single
    identity across the two calendar days it spans. Because the broker enforces
    uniqueness of ``client_order_id``, a duplicate submission after a crash is
    rejected by the venue rather than by hopeful bookkeeping on our side.

    ``attempt`` is bumped **only** when an order is known to have been rejected.
    A timeout must never bump it: the order may well have been accepted, and
    resubmitting under a fresh id is precisely how you end up with two positions.
    """
    cid = f"{prefix}-{symbol.upper()}-{trade_date.isoformat()}-{leg.upper()}-{attempt}"
    if len(cid) > 128:
        raise ValueError(f"client_order_id too long ({len(cid)}): {cid}")
    return cid


def parse_client_order_id(cid: str) -> dict[str, str] | None:
    """Inverse of :func:`make_client_order_id`; ``None`` if not one of ours."""
    parts = cid.split("-")
    if len(parts) != 7:
        return None
    prefix, symbol, y, m, d, leg, attempt = parts
    try:
        trade_date = dt.date(int(y), int(m), int(d))
    except ValueError:
        return None
    return {
        "prefix": prefix, "symbol": symbol, "trade_date": trade_date.isoformat(),
        "leg": leg, "attempt": attempt,
    }
