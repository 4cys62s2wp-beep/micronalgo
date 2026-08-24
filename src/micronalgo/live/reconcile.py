"""Reconciliation: the broker is the truth, local state is a cache.

Run at every start and periodically thereafter. Three classes of disagreement
matter, and they are not symmetric:

**State says holding, broker is flat.**
    Benign in almost every case: the exit filled while the process was down, or
    a human closed it. Resolve by looking up the exit order; close the trade out
    either way.

**State says flat, broker holds a position.**
    Dangerous. Trading on top of a position of unknown provenance is how one
    mistake becomes several. If the position can be attributed to one of our own
    client order ids the trade is adopted and continues normally. If it cannot,
    the bot **halts** and asks for a human. Auto-liquidating someone else's
    position would be a worse failure than stopping.

**An order is in flight that state has not caught up with.**
    Sync the status from the broker and continue.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .audit import AuditLog, get_logger
from .broker import Broker, BrokerError, Order, OrderStatus, parse_client_order_id
from .state import BotState, LegState, Phase

log = get_logger("reconcile")


@dataclass
class ReconcileResult:
    changes: list[str] = field(default_factory=list)
    halted: bool = False
    broker_qty: float = 0.0
    adopted: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        self.changes.append(text)

    @property
    def clean(self) -> bool:
        return not self.changes and not self.halted


def _sync_leg(leg: LegState, order: Order) -> None:
    leg.broker_order_id = order.broker_order_id or leg.broker_order_id
    leg.status = order.status.value
    leg.filled_qty = order.filled_qty
    leg.filled_avg_price = order.filled_avg_price
    if order.filled_at:
        leg.filled_at = order.filled_at.isoformat()


def reconcile(
    broker: Broker,
    state: BotState,
    *,
    symbol: str,
    audit: AuditLog | None = None,
    auto_adopt: bool = True,
) -> ReconcileResult:
    """Bring local state in line with the broker. Returns what changed."""
    result = ReconcileResult()

    try:
        position = broker.get_position(symbol)
    except BrokerError as exc:
        result.add(f"could not read broker position: {exc}")
        state.halt(f"reconciliation failed: {exc}")
        result.halted = True
        return result

    broker_qty = position.qty if position else 0.0
    result.broker_qty = broker_qty

    # 1. Sync every leg that state believes is in flight or filled.
    for trade in sorted(state.trades.values(), key=lambda t: t.trade_date):
        for name, leg in (("entry", trade.entry), ("exit", trade.exit)):
            if not leg.client_order_id or OrderStatus.parse(leg.status).is_terminal:
                continue
            try:
                order = broker.get_order_by_client_id(leg.client_order_id)
            except BrokerError as exc:
                result.add(f"{trade.trade_date}/{name}: order lookup failed: {exc}")
                continue
            if order is None:
                if leg.status:
                    result.add(f"{trade.trade_date}/{name}: order {leg.client_order_id} unknown to broker")
                continue
            before = leg.status
            _sync_leg(leg, order)
            if before != leg.status:
                result.add(f"{trade.trade_date}/{name}: {before or 'unsent'} -> {leg.status}")
                trade.note(f"{name} status synced to {leg.status}")
        _advance_phase(trade)

    # 2. State believes it holds something the broker does not have.
    believed = state.open_trades()
    believed_qty = sum(t.entry.filled_qty for t in believed)
    if believed and abs(broker_qty) < 1e-9:
        for trade in believed:
            exit_order = None
            if trade.exit.client_order_id:
                try:
                    exit_order = broker.get_order_by_client_id(trade.exit.client_order_id)
                except BrokerError:
                    exit_order = None
            if exit_order is not None and exit_order.is_filled:
                _sync_leg(trade.exit, exit_order)
                trade.phase = Phase.CLOSED
                result.add(f"{trade.trade_date}: exit had filled while offline -> CLOSED")
            else:
                trade.phase = Phase.CLOSED
                trade.note("broker is flat but no filled exit order was found; assuming manual close")
                result.add(
                    f"{trade.trade_date}: broker flat with no filled exit order -> CLOSED "
                    "(position appears to have been closed outside the bot)"
                )

    # 3. Broker holds something state does not know about.
    elif abs(broker_qty) > 1e-9 and abs(broker_qty - believed_qty) > 1e-9:
        owner = _attribute_position(broker, symbol, state)
        if owner is not None and auto_adopt:
            trade = state.trade(owner)
            trade.phase = Phase.ENTRY_FILLED
            trade.entry.filled_qty = abs(broker_qty)
            trade.note(f"adopted an untracked broker position of {broker_qty:g} shares")
            result.adopted.append(owner)
            result.add(f"{owner}: adopted untracked position of {broker_qty:g} shares")
        else:
            state.halt(
                f"broker holds {broker_qty:g} {symbol} that this bot did not open "
                f"(state expects {believed_qty:g}). Refusing to trade on top of an unattributed position."
            )
            result.halted = True
            result.add(state.halt_reason)

    if audit is not None and not result.clean:
        audit.emit(
            "reconcile",
            broker_qty=broker_qty,
            believed_qty=believed_qty,
            changes=result.changes,
            halted=result.halted,
            adopted=result.adopted,
        )
    for line in result.changes:
        log.warning("reconcile: %s", line)
    return result


def _attribute_position(broker: Broker, symbol: str, state: BotState) -> str | None:
    """Try to find which of our trade dates a stray position belongs to.

    Looks through recent orders the broker knows about for a client order id
    minted by this package. Returns the trade date, or ``None`` if the position
    cannot be attributed -- in which case the caller must halt rather than guess.
    """
    try:
        orders = broker.list_open_orders(symbol)
    except BrokerError:
        orders = []
    candidates: list[str] = []
    for order in orders:
        parsed = parse_client_order_id(order.client_order_id)
        if parsed and parsed["symbol"] == symbol.upper():
            candidates.append(parsed["trade_date"])
    if candidates:
        return sorted(candidates)[-1]

    # Fall back to a trade this bot recently submitted an entry for.
    recent = [
        t for t in state.trades.values()
        if t.entry.client_order_id and t.phase in {Phase.ENTRY_SUBMITTED, Phase.ENTRY_FILLED}
    ]
    if recent:
        return sorted(recent, key=lambda t: t.trade_date)[-1].trade_date
    return None


def _advance_phase(trade) -> None:
    """Derive the phase from the leg statuses. Pure function of observed facts."""
    entry_status = OrderStatus.parse(trade.entry.status) if trade.entry.status else None
    exit_status = OrderStatus.parse(trade.exit.status) if trade.exit.status else None

    if exit_status is OrderStatus.FILLED:
        trade.phase = Phase.CLOSED
    elif exit_status in {OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED}:
        trade.phase = Phase.EXIT_FAILED
    elif exit_status is not None and exit_status.is_open:
        trade.phase = Phase.EXIT_SUBMITTED
    elif entry_status is OrderStatus.FILLED:
        trade.phase = Phase.ENTRY_FILLED
    elif entry_status in {OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED}:
        trade.phase = Phase.ENTRY_FAILED
    elif entry_status is not None and entry_status.is_open:
        trade.phase = Phase.ENTRY_SUBMITTED


def stale_open_orders(broker: Broker, symbol: str, *, older_than_min: int = 120,
                      now: dt.datetime | None = None) -> list[Order]:
    """Open orders that have been sitting far longer than they should.

    An auction order lives at most a few hours. Anything older is a leftover
    that will fire at an unexpected moment, and should be cancelled.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for order in broker.list_open_orders(symbol):
        if order.submitted_at is None:
            continue
        submitted = order.submitted_at
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=dt.timezone.utc)
        if (now - submitted) > dt.timedelta(minutes=older_than_min):
            out.append(order)
    return out
