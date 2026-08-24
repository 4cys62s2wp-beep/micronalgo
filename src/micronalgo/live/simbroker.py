"""In-process simulated broker.

Two jobs:

1. **Deterministic testing.** The live state machine is exercised end to end --
   including restarts, missed cutoffs, rejections and partial fills -- with no
   network and no wall-clock dependence, by driving a virtual clock forward.
2. **An honest offline dry run.** A user with no broker account can watch the
   scheduler make real decisions against real historical prices before ever
   creating one.

Fill model
----------
``cls`` fills at that session's official close, ``opg`` at the next session's
official open. That mirrors what a genuine auction order does, and it is the
same price the backtest uses -- so a divergence between simulated and
backtested P&L is a bug in the state machine, not a modelling difference. Plain
``day`` market orders fill at the last price with a configurable spread cost,
which is what makes the "missed the auction" path visibly worse.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field

import pandas as pd

from ..calendar_nyse import NY, Calendar, research_calendar
from .broker import (
    Account,
    Clock,
    DuplicateOrderError,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PermanentBrokerError,
    Position,
    TimeInForce,
)


@dataclass
class SimBroker:
    """Simulated broker driven by a bar frame and an explicit virtual clock."""

    bars: pd.DataFrame
    cash: float = 100_000.0
    calendar: Calendar = field(default_factory=research_calendar)
    spread_bps: float = 2.0
    reject_reason: str | None = None
    partial_fill_fraction: float | None = None
    start_at: dt.datetime | None = None
    name: str = "sim"

    _now: dt.datetime = field(default=None, init=False)  # type: ignore[assignment]
    _orders: dict[str, Order] = field(default_factory=dict, init=False)
    _by_broker_id: dict[str, Order] = field(default_factory=dict, init=False)
    _position_qty: float = field(default=0.0, init=False)
    _position_cost: float = field(default=0.0, init=False)
    _ids = itertools.count(1)
    fills: list[Order] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.bars.empty:
            raise ValueError("SimBroker needs a non-empty bar frame")
        if self.start_at is not None:
            self._now = self.start_at if self.start_at.tzinfo else self.start_at.replace(tzinfo=NY)
        if self._now is None:
            # Start at midnight of the first session so that pre-open activity
            # (the opening-auction submission window) can be exercised.
            self._now = dt.datetime.combine(self.bars.index[0].date(), dt.time(0, 0), tzinfo=NY)
        self._initial_cash = self.cash

    # ------------------------------------------------------------------ clock
    @property
    def now(self) -> dt.datetime:
        return self._now

    def set_now(self, when: dt.datetime) -> None:
        """Advance the virtual clock, settling any auction orders it passes."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=NY)
        if when < self._now:
            raise ValueError(f"virtual clock cannot go backwards: {self._now} -> {when}")
        self._settle_between(self._now, when)
        self._now = when

    def advance(self, **delta) -> None:
        self.set_now(self._now + dt.timedelta(**delta))

    # ------------------------------------------------------------------ broker API
    def get_clock(self) -> Clock:
        sess = self.calendar.session(self._now.date())
        is_open = bool(sess and sess.open_dt() <= self._now < sess.close_dt())
        nxt = self.calendar.next_session(self._now.date())
        return Clock(
            timestamp=self._now,
            is_open=is_open,
            next_open=sess.open_dt() if sess and self._now < sess.open_dt() else nxt.open_dt(),
            next_close=sess.close_dt() if sess and self._now < sess.close_dt() else nxt.close_dt(),
        )

    def get_calendar(self, start: dt.date, end: dt.date) -> dict[dt.date, tuple[dt.time, dt.time]]:
        return {s.date: (s.open_time, s.close_time) for s in self.calendar.sessions_between(start, end)}

    def get_account(self) -> Account:
        mark = self._last_price_or_nan()
        equity = self.cash + self._position_qty * (mark if mark == mark else 0.0)
        return Account(
            equity=equity,
            cash=self.cash,
            buying_power=max(equity, 0.0),
            last_equity=self._initial_cash,
        )

    def get_position(self, symbol: str) -> Position | None:
        if abs(self._position_qty) < 1e-9:
            return None
        mark = self._last_price_or_nan()
        avg = self._position_cost / self._position_qty if self._position_qty else 0.0
        mv = self._position_qty * (mark if mark == mark else avg)
        return Position(symbol.upper(), self._position_qty, avg, mv, mv - self._position_cost)

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._orders.values() if o.status.is_open]

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

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
    ) -> Order:
        if client_order_id in self._orders:
            raise DuplicateOrderError(f"client_order_id must be unique: {client_order_id} already exists")
        if qty <= 0:
            raise PermanentBrokerError(f"qty must be positive, got {qty}")
        if self.reject_reason:
            order = Order(
                client_order_id=client_order_id, symbol=symbol.upper(), side=side, qty=qty,
                order_type=order_type, time_in_force=time_in_force, limit_price=limit_price,
                broker_order_id=f"sim-{next(self._ids)}", status=OrderStatus.REJECTED,
                submitted_at=self._now, raw={"reject_reason": self.reject_reason},
            )
            self._orders[client_order_id] = order
            self._by_broker_id[order.broker_order_id] = order
            return order

        self._check_auction_cutoff(time_in_force)

        order = Order(
            client_order_id=client_order_id, symbol=symbol.upper(), side=side, qty=qty,
            order_type=order_type, time_in_force=time_in_force, limit_price=limit_price,
            broker_order_id=f"sim-{next(self._ids)}", status=OrderStatus.ACCEPTED,
            submitted_at=self._now,
        )
        self._orders[client_order_id] = order
        self._by_broker_id[order.broker_order_id] = order

        if time_in_force in (TimeInForce.DAY, TimeInForce.IOC, TimeInForce.FOK, TimeInForce.GTC):
            clock = self.get_clock()
            if clock.is_open:
                self._fill(order, self._marketable_price(side))
        return order

    def cancel_order(self, broker_order_id: str) -> None:
        order = self._by_broker_id.get(broker_order_id)
        if order and order.status.is_open:
            order.status = OrderStatus.CANCELED

    def get_last_price(self, symbol: str) -> float:
        price = self._last_price_or_nan()
        if price != price:
            raise PermanentBrokerError("no price available at the current virtual time")
        return float(price)

    def is_tradable(self, symbol: str) -> bool:
        return True

    def supports_fractional(self, symbol: str) -> bool:
        return False

    # ------------------------------------------------------------------ internals
    def _check_auction_cutoff(self, tif: TimeInForce) -> None:
        """Reject auction orders submitted too late, as the venue would."""
        sess = self.calendar.session(self._now.date())
        if tif is TimeInForce.CLS:
            if sess is None:
                raise PermanentBrokerError("market closed: cannot submit a closing-auction order")
            if self._now > sess.close_dt() - dt.timedelta(minutes=10):
                raise PermanentBrokerError("closing-auction order submitted after the cutoff")
        if tif is TimeInForce.OPG:
            target = sess if (sess and self._now < sess.open_dt()) else self.calendar.next_session(self._now.date())
            if self._now > target.open_dt() - dt.timedelta(minutes=2):
                raise PermanentBrokerError("opening-auction order submitted after the cutoff")

    def _settle_between(self, start: dt.datetime, end: dt.datetime) -> None:
        """Fill any auction order whose auction time falls in ``(start, end]``."""
        for order in list(self._orders.values()):
            if not order.status.is_open:
                continue
            auction_at, price = self._auction_event(order)
            if auction_at is None or price is None:
                continue
            if start < auction_at <= end:
                self._fill(order, price, when=auction_at)

    def _auction_event(self, order: Order) -> tuple[dt.datetime | None, float | None]:
        if order.time_in_force is TimeInForce.CLS:
            day = (order.submitted_at or self._now).date()
            sess = self.calendar.session(day)
            if sess is None:
                return None, None
            return sess.close_dt(), self._bar_value(day, "raw_close")
        if order.time_in_force is TimeInForce.OPG:
            submitted = order.submitted_at or self._now
            sess = self.calendar.session(submitted.date())
            target = sess if (sess and submitted < sess.open_dt()) else self.calendar.next_session(submitted.date())
            return target.open_dt(), self._bar_value(target.date, "raw_open")
        return None, None

    def _fill(self, order: Order, price: float | None, when: dt.datetime | None = None) -> None:
        if price is None or price != price or price <= 0:
            order.status = OrderStatus.EXPIRED
            order.raw["note"] = "no price available for the auction; order expired"
            return
        qty = order.qty
        if self.partial_fill_fraction is not None:
            qty = max(round(order.qty * self.partial_fill_fraction), 1)
        signed = qty if order.side is OrderSide.BUY else -qty
        self.cash -= signed * price
        self._position_cost += signed * price
        self._position_qty += signed
        if abs(self._position_qty) < 1e-9:
            self._position_qty, self._position_cost = 0.0, 0.0

        order.filled_qty = qty
        order.filled_avg_price = price
        order.filled_at = when or self._now
        order.status = OrderStatus.FILLED if qty >= order.qty else OrderStatus.PARTIALLY_FILLED
        self.fills.append(order)

    def _marketable_price(self, side: OrderSide) -> float | None:
        base = self._last_price_or_nan()
        if base != base:
            return None
        edge = self.spread_bps * 1e-4 / 2.0
        return base * (1.0 + edge) if side is OrderSide.BUY else base * (1.0 - edge)

    def _bar_value(self, day: dt.date, column: str) -> float | None:
        ts = pd.Timestamp(day)
        if ts not in self.bars.index:
            return None
        value = self.bars.at[ts, column]
        return float(value) if value == value else None

    def _last_price_or_nan(self) -> float:
        """Simulated last trade price at the current virtual time.

        A daily bar carries no intraday path, so one is assumed: the price moves
        linearly from the session's open to its close. That matters for one
        thing only -- the *reference price used to size the order* at
        ``close - 15m``, which in reality is within a fraction of a percent of
        the auction print. Returning the session's open for the entire day (the
        obvious shortcut) would size every position off a stale price and make
        the simulator disagree with the backtest for a reason that has nothing
        to do with the strategy.

        The interpolation is never used as a *fill* price: auction orders fill
        at the recorded open or close exactly.
        """
        ts = pd.Timestamp(self._now.date())
        idx = self.bars.index
        pos = idx.searchsorted(ts, side="right") - 1
        if pos < 0:
            return float("nan")
        row = self.bars.iloc[pos]
        sess = self.calendar.session(self._now.date())

        if idx[pos] == ts and sess is not None and self._now < sess.close_dt():
            if self._now < sess.open_dt():
                return float(self.bars.iloc[max(pos - 1, 0)]["raw_close"])
            span = (sess.close_dt() - sess.open_dt()).total_seconds()
            done = (self._now - sess.open_dt()).total_seconds()
            frac = min(max(done / span, 0.0), 1.0) if span > 0 else 1.0
            o, c = float(row["raw_open"]), float(row["raw_close"])
            return o + (c - o) * frac
        return float(row["raw_close"])
