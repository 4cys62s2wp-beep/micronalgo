"""The bot.

Everything happens in :meth:`OvernightBot.tick`, which is **idempotent**: calling
it once a minute, once an hour, or fifty times in a row produces the same
result, because each action first asks the persisted state whether it has
already been done. That property is what makes the system survive restarts,
scheduler misfires, laptop sleep and container evictions without a bespoke
recovery path for each.

The trading day, in exchange local time
---------------------------------------
    T-60m before the open   submit the market-on-open SELL for the position
                            held overnight
    open + 5m               verify the account is flat
    close - 15m             submit the market-on-close BUY
    close - 10m             hard cutoff; past it the session is skipped
    close + 5m              verify the fill and record it

All four are offsets from the *session's own* open and close, so a 13:00 ET
half-day shifts the closing-auction work three hours earlier automatically.

What the bot will not do
------------------------
* It will not chase a missed closing auction with a market order (default),
  because the auction print is the reason the edge survives costs.
* It will not trade on top of a position it cannot attribute to itself.
* It will not hold through a session it meant to exit without saying so loudly:
  the intraday leg is the one with the negative expected return.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..calendar_nyse import NY, Calendar, CalendarError, Session, now_ny
from ..config import Settings
from .audit import AuditLog, get_logger
from .broker import (
    Broker,
    BrokerError,
    DuplicateOrderError,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PermanentBrokerError,
    TimeInForce,
    TransientBrokerError,
    make_client_order_id,
)
from .reconcile import reconcile
from .risk import ErrorBudget, RiskContext, evaluate
from .state import BotState, LegState, Phase, load, save
from .strategy import should_trade_session, target_shares

log = get_logger("runner")

ORDER_PREFIX = "mna"


@dataclass
class Action:
    kind: str
    detail: str
    trade_date: str = ""
    order: Order | None = None
    data: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


class OvernightBot:
    """Buy the closing auction, sell the opening auction, hold nothing by day."""

    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        *,
        calendar: Calendar,
        bars: pd.DataFrame | None = None,
        state_path: Path | str | None = None,
        audit: AuditLog | None = None,
        price_feed: Callable[[], float | None] | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.calendar = calendar
        self.bars = bars
        self.state_path = Path(state_path or Path(settings.state_dir) / "state.json")
        self.audit = audit or AuditLog(Path(settings.log_dir) / "audit.jsonl")
        self.state: BotState = load(self.state_path, symbol=settings.symbol)
        self.errors = ErrorBudget(settings.api_error_budget, settings.api_error_window_min)
        self.price_feed = price_feed
        self._reconciled_for: dt.date | None = None

    # ------------------------------------------------------------------ #
    def save(self) -> None:
        self.state.prune()
        save(self.state, self.state_path)

    def _emit(self, event: str, **fields) -> None:
        self.audit.emit(event, symbol=self.settings.symbol, **fields)

    # ------------------------------------------------------------------ #
    def tick(self, now: dt.datetime | None = None) -> list[Action]:
        """Do whatever is due and not yet done. Safe to call at any moment."""
        now = now or now_ny()
        if now.tzinfo is None:
            now = now.replace(tzinfo=NY)
        actions: list[Action] = []

        if self._reconciled_for != now.date():
            actions.extend(self._reconcile(now))
            self._reconciled_for = now.date()

        try:
            session = self.calendar.session(now.date())
        except CalendarError as exc:
            # Fail closed: an unknown calendar means an unknown auction time.
            actions.append(Action("halt", f"calendar unresolved for {now.date()}: {exc}"))
            self.state.halt(f"calendar unresolved: {exc}")
            self._emit("calendar_error", error=str(exc))
            self.save()
            return actions

        if session is None:
            actions.append(Action("idle", f"{now.date()} is not a trading session"))
            self.save()
            return actions

        actions.extend(self._maybe_exit(now, session))
        actions.extend(self._verify_after_open(now, session))
        actions.extend(self._maybe_enter(now, session))
        actions.extend(self._verify_after_close(now, session))

        self.save()
        return actions

    # ------------------------------------------------------------------ #
    def _reconcile(self, now: dt.datetime) -> list[Action]:
        result = reconcile(
            self.broker, self.state, symbol=self.settings.symbol, audit=self.audit,
        )
        out = [Action("reconcile", c) for c in result.changes]
        if result.halted:
            out.append(Action("halt", self.state.halt_reason))
        return out

    # ------------------------------------------------------------------ exit
    def _maybe_exit(self, now: dt.datetime, session: Session) -> list[Action]:
        """Sell any position that was opened at a *previous* session's close."""
        actions: list[Action] = []
        submit_at, cutoff = self.settings.exit_window(session.open_dt())
        today_key = now.date().isoformat()

        pending = [t for t in sorted(self.state.open_trades(), key=lambda t: t.trade_date)
                   if t.trade_date < today_key and t.entry.filled_qty > 0]
        if not pending:
            return actions

        # The broker's position is the truth. Selling the journal's share count
        # can oversell (a partial fill, or a manual close) and leave a short.
        try:
            position = self.broker.get_position(self.settings.symbol)
        except BrokerError as exc:
            self.errors.record(now)
            self._emit("broker_error", stage="exit_position", error=str(exc))
            return [Action("retry", f"cannot read the position before exiting: {exc}")]
        remaining = position.qty if position else 0.0

        if remaining <= 0 and any(t.phase is not Phase.EXIT_SUBMITTED for t in pending):
            for trade in pending:
                if trade.phase is Phase.EXIT_SUBMITTED:
                    continue
                trade.phase = Phase.CLOSED
                trade.note("broker reports no position at exit time; nothing to sell")
                actions.append(Action("sync", f"{trade.trade_date}: broker flat, marking closed",
                                      trade.trade_date))
            return actions

        for trade in pending:
            qty = min(trade.entry.filled_qty, remaining)
            if trade.phase is not Phase.EXIT_SUBMITTED and qty < trade.entry.filled_qty:
                trade.note(
                    f"journal says {trade.entry.filled_qty:g} shares but the broker holds "
                    f"{remaining:g}; selling the broker's number"
                )
            if qty <= 0:
                continue

            if trade.phase is Phase.EXIT_SUBMITTED:
                actions.extend(self._sync_leg_from_broker(trade, "exit"))
                continue

            if now < submit_at:
                continue

            in_window = now <= cutoff
            if in_window:
                tif, otype = TimeInForce.OPG, OrderType.MARKET
                why = "opening auction"
            elif self.settings.on_missed_exit == "market_at_open" and self._market_is_open(now, session):
                tif, otype = TimeInForce.DAY, OrderType.MARKET
                why = (
                    "MISSED the opening-auction window; sending a plain market order rather than "
                    "holding into the session (the intraday leg has a negative expected return)"
                )
            else:
                if not trade.notes or "waiting" not in trade.notes[-1]:
                    trade.note("missed the opening-auction window; waiting per on_missed_exit=hold_and_alert")
                    self._emit("exit_missed_window", trade_date=trade.trade_date, qty=qty)
                actions.append(
                    Action("alert", f"{trade.trade_date}: missed the exit window and holding {qty:g} shares",
                           trade_date=trade.trade_date)
                )
                continue

            if self.settings.dry_run:
                actions.append(Action("dry_run", f"would SELL {qty:g} ({why})", trade.trade_date))
                self._emit("dry_run_exit", trade_date=trade.trade_date, qty=qty, reason=why)
                trade.phase = Phase.CLOSED
                trade.note(f"dry run: would have sold {qty:g} via {why}")
                continue

            trade.exit.client_order_id = trade.exit.client_order_id or make_client_order_id(
                ORDER_PREFIX, self.settings.symbol, dt.date.fromisoformat(trade.trade_date),
                "sell", trade.exit.attempt,
            )
            trade.exit.qty = qty
            order = self._submit(trade.exit, side=OrderSide.SELL, qty=qty, order_type=otype, tif=tif)
            if order is None:
                actions.append(Action("retry", f"{trade.trade_date}: exit submission unresolved, will retry",
                                      trade.trade_date))
                continue

            trade.phase = Phase.EXIT_SUBMITTED if order.status.is_open else (
                Phase.CLOSED if order.is_filled else Phase.EXIT_FAILED
            )
            remaining = max(remaining - qty, 0.0)
            trade.note(f"exit submitted ({why}): {order.summary()}")
            self._emit("exit_submitted", trade_date=trade.trade_date, qty=qty, tif=tif.value,
                       reason=why, order=order.summary())
            actions.append(Action("exit_submitted", order.summary(), trade.trade_date, order))

        return actions

    def _verify_after_open(self, now: dt.datetime, session: Session) -> list[Action]:
        deadline = session.open_dt() + dt.timedelta(minutes=self.settings.verify_after_open_min)
        if now < deadline:
            return []
        actions: list[Action] = []
        today_key = now.date().isoformat()
        for trade in sorted(self.state.open_trades(), key=lambda t: t.trade_date):
            if trade.trade_date >= today_key:
                continue
            actions.extend(self._sync_leg_from_broker(trade, "exit"))

            if trade.phase is Phase.EXIT_FAILED and self._market_is_open(now, session):
                actions.extend(self._escalate_exit(trade, now))

            if trade.phase.holds_position:
                msg = (
                    f"{trade.trade_date}: still holding {trade.entry.filled_qty:g} shares "
                    f"{self.settings.verify_after_open_min}m after the open -- the intraday leg is "
                    "exactly the exposure this strategy exists to avoid"
                )
                if not any("still holding" in n for n in trade.notes[-3:]):
                    trade.note(msg)
                    self._emit("exit_not_confirmed", trade_date=trade.trade_date)
                actions.append(Action("alert", msg, trade.trade_date))
        return actions

    def _escalate_exit(self, trade, now: dt.datetime) -> list[Action]:
        """An auction exit that was rejected or expired must not become a hold.

        Being long into the session is the leg whose historical expected return
        is negative -- the exact exposure this strategy exists to avoid. So a
        dead opening-auction order escalates to a plain market order rather than
        waiting for tomorrow.
        """
        if self.settings.dry_run:
            return [Action("dry_run", f"would escalate the failed exit for {trade.trade_date}",
                           trade.trade_date)]
        try:
            position = self.broker.get_position(self.settings.symbol)
        except BrokerError as exc:
            self.errors.record(now)
            return [Action("retry", f"cannot read the position to escalate: {exc}", trade.trade_date)]

        qty = min(trade.entry.filled_qty, position.qty if position else 0.0)
        if qty <= 0:
            trade.phase = Phase.CLOSED
            trade.note("exit order failed but the broker is already flat")
            return [Action("sync", f"{trade.trade_date}: already flat, closed", trade.trade_date)]

        trade.exit.attempt += 1
        trade.exit.client_order_id = make_client_order_id(
            ORDER_PREFIX, self.settings.symbol, dt.date.fromisoformat(trade.trade_date),
            "sell", trade.exit.attempt,
        )
        order = self._submit(trade.exit, side=OrderSide.SELL, qty=qty,
                             order_type=OrderType.MARKET, tif=TimeInForce.DAY)
        if order is None:
            return [Action("retry", f"{trade.trade_date}: exit escalation unresolved", trade.trade_date)]

        trade.phase = Phase.EXIT_SUBMITTED if order.status.is_open else (
            Phase.CLOSED if order.is_filled else Phase.EXIT_FAILED
        )
        trade.note(f"escalated the failed auction exit to a market order: {order.summary()}")
        self._emit("exit_escalated", trade_date=trade.trade_date, qty=qty,
                   attempt=trade.exit.attempt, order=order.summary())
        return [Action("exit_escalated", order.summary(), trade.trade_date, order)]

    # ------------------------------------------------------------------ entry
    def _maybe_enter(self, now: dt.datetime, session: Session) -> list[Action]:
        actions: list[Action] = []
        trade = self.state.trade(now.date())
        if trade.phase is not Phase.PENDING:
            if trade.phase is Phase.ENTRY_SUBMITTED:
                actions.extend(self._sync_leg_from_broker(trade, "entry"))
            return actions

        submit_at, cutoff = self.settings.entry_window(session.close_dt())
        if now < submit_at:
            return actions

        if now > cutoff:
            if self.settings.on_missed_entry == "market_before_close" and self._market_is_open(now, session):
                pass  # fall through and send a plain market order
            else:
                trade.phase = Phase.SKIPPED
                trade.skip_reason = (
                    f"missed the closing-auction window (submit {submit_at:%H:%M}, cutoff {cutoff:%H:%M}, "
                    f"now {now:%H:%M}); not chasing the print"
                )
                self._emit("entry_skipped", trade_date=trade.trade_date, reason=trade.skip_reason)
                return [Action("skipped", trade.skip_reason, trade.trade_date)]

        allow, reason = should_trade_session(self.settings, self.bars, now.date())
        if not allow:
            trade.phase = Phase.SKIPPED
            trade.skip_reason = reason
            self._emit("entry_skipped", trade_date=trade.trade_date, reason=reason)
            return [Action("skipped", reason, trade.trade_date)]
        if reason:
            actions.append(Action("warning", reason, trade.trade_date))

        try:
            account = self.broker.get_account()
            reference = self._reference_price()
            tradable = self.broker.is_tradable(self.settings.symbol)
        except BrokerError as exc:
            self.errors.record(now)
            self._emit("broker_error", stage="entry_prep", error=str(exc))
            return [Action("retry", f"broker unavailable while preparing entry: {exc}", trade.trade_date)]

        self.state.last_equity = account.equity
        self.state.equity_peak = max(self.state.equity_peak, account.equity)

        adv = float(self.bars["volume"].tail(20).mean()) if self.bars is not None and "volume" in self.bars else None
        sizing = target_shares(account.equity, reference, self.settings, adv=adv)

        verdict = evaluate(
            self.settings, self.state,
            RiskContext(
                account=account,
                reference_price=reference,
                last_known_close=self._last_known_close(),
                data_age_days=self._data_age_days(now),
                asset_tradable=tradable,
                intended_notional=sizing.notional,
                intended_shares=sizing.shares,
                now=now,
            ),
            error_budget=self.errors,
        )
        for w in verdict.warnings:
            actions.append(Action("warning", w, trade.trade_date))

        if not verdict.allow or not sizing.tradable:
            trade.phase = Phase.SKIPPED
            trade.skip_reason = verdict.reason if not verdict.allow else sizing.reason
            self._emit("entry_blocked", trade_date=trade.trade_date, blocks=verdict.blocks,
                       sizing_reason=sizing.reason)
            actions.append(Action("skipped", trade.skip_reason, trade.trade_date))
            return actions

        trade.intended_qty = sizing.shares
        trade.reference_price = reference

        if self.settings.dry_run:
            trade.phase = Phase.SKIPPED
            trade.skip_reason = "dry run"
            trade.note(f"dry run: would have bought {sizing.shares:g} @ ~{reference:.2f}")
            self._emit("dry_run_entry", trade_date=trade.trade_date, qty=sizing.shares, reference=reference)
            return [Action("dry_run", f"would BUY {sizing.shares:g} @ ~{reference:.2f} on the close",
                           trade.trade_date)]

        in_auction_window = now <= cutoff
        if in_auction_window and self.settings.entry_order_type == "loc":
            otype, tif = OrderType.LIMIT, TimeInForce.CLS
            limit = reference * (1.0 + self.settings.loc_limit_offset_bps * 1e-4)
        elif in_auction_window:
            otype, tif, limit = OrderType.MARKET, TimeInForce.CLS, None
        else:
            otype, tif, limit = OrderType.MARKET, TimeInForce.DAY, None

        trade.entry.client_order_id = trade.entry.client_order_id or make_client_order_id(
            ORDER_PREFIX, self.settings.symbol, now.date(), "buy", trade.entry.attempt
        )
        trade.entry.qty = sizing.shares
        order = self._submit(
            trade.entry, side=OrderSide.BUY, qty=sizing.shares, order_type=otype, tif=tif, limit_price=limit
        )
        if order is None:
            return [Action("retry", "entry submission unresolved, will retry with the same order id",
                           trade.trade_date)]

        trade.phase = Phase.ENTRY_SUBMITTED if order.status.is_open else (
            Phase.ENTRY_FILLED if order.is_filled else Phase.ENTRY_FAILED
        )
        trade.note(f"entry submitted: {order.summary()}")
        self._emit("entry_submitted", trade_date=trade.trade_date, qty=sizing.shares,
                   reference=reference, tif=tif.value, order=order.summary())
        actions.append(Action("entry_submitted", order.summary(), trade.trade_date, order))
        return actions

    def _verify_after_close(self, now: dt.datetime, session: Session) -> list[Action]:
        deadline = session.close_dt() + dt.timedelta(minutes=self.settings.verify_after_close_min)
        if now < deadline:
            return []
        trade = self.state.trade(now.date())
        if trade.phase is not Phase.ENTRY_SUBMITTED:
            return []
        actions = self._sync_leg_from_broker(trade, "entry")
        if trade.phase is Phase.ENTRY_FILLED:
            self._emit("entry_filled", trade_date=trade.trade_date,
                       qty=trade.entry.filled_qty, price=trade.entry.filled_avg_price)
            actions.append(Action(
                "entry_filled",
                f"filled {trade.entry.filled_qty:g} @ {trade.entry.filled_avg_price}", trade.trade_date))
        elif trade.phase in {Phase.ENTRY_FAILED, Phase.ENTRY_SUBMITTED}:
            actions.append(Action(
                "alert", f"{trade.trade_date}: entry order did not fill in the closing auction "
                         f"(status={trade.entry.status})", trade.trade_date))
        return actions

    # ------------------------------------------------------------------ helpers
    def _submit(
        self, leg: LegState, *, side: OrderSide, qty: float, order_type: OrderType,
        tif: TimeInForce, limit_price: float | None = None,
    ) -> Order | None:
        """Submit idempotently.

        A ``DuplicateOrderError`` means a previous attempt already reached the
        venue -- adopt it. A transient failure leaves the state unknown, so we
        look the order up by its client id; if it is not there we return
        ``None`` and the next tick retries **with the same id**. Minting a new
        id after a timeout is exactly how one intended position becomes two.
        """
        cid = leg.client_order_id
        try:
            order = self.broker.submit_order(
                symbol=self.settings.symbol, side=side, qty=qty, order_type=order_type,
                time_in_force=tif, limit_price=limit_price, client_order_id=cid,
            )
        except DuplicateOrderError:
            order = self.broker.get_order_by_client_id(cid)
            if order is None:
                self.errors.record()
                return None
            log.info("adopted existing order for %s", cid)
        except TransientBrokerError as exc:
            self.errors.record()
            self._emit("submit_transient_error", client_order_id=cid, error=str(exc))
            try:
                order = self.broker.get_order_by_client_id(cid)
            except BrokerError:
                order = None
            if order is None:
                log.warning("submit failed for %s (%s); will retry with the same id", cid, exc)
                return None
        except PermanentBrokerError as exc:
            self.errors.record()
            self._emit("submit_rejected", client_order_id=cid, error=str(exc))
            leg.status = OrderStatus.REJECTED.value
            leg.note = str(exc)
            leg.attempt += 1  # a definite rejection is the only thing that bumps the attempt
            leg.client_order_id = ""
            return None

        leg.broker_order_id = order.broker_order_id
        leg.status = order.status.value
        leg.filled_qty = order.filled_qty
        leg.filled_avg_price = order.filled_avg_price
        leg.submitted_at = (order.submitted_at or dt.datetime.now(dt.timezone.utc)).isoformat()
        return order

    def _sync_leg_from_broker(self, trade, which: str) -> list[Action]:
        leg = getattr(trade, which)
        if not leg.client_order_id:
            return []
        try:
            order = self.broker.get_order_by_client_id(leg.client_order_id)
        except BrokerError as exc:
            self.errors.record()
            return [Action("retry", f"could not sync {which} order: {exc}", trade.trade_date)]
        if order is None:
            return []
        before = leg.status
        leg.status = order.status.value
        leg.filled_qty = order.filled_qty
        leg.filled_avg_price = order.filled_avg_price
        if order.filled_at:
            leg.filled_at = order.filled_at.isoformat()

        if which == "entry":
            if order.is_filled:
                trade.phase = Phase.ENTRY_FILLED
            elif order.status.is_terminal:
                trade.phase = Phase.ENTRY_FAILED
        else:
            if order.is_filled:
                trade.phase = Phase.CLOSED
                self._record_pnl(trade)
            elif order.status.is_terminal:
                trade.phase = Phase.EXIT_FAILED

        if before != leg.status:
            trade.note(f"{which} {before or 'unsent'} -> {leg.status}")
            return [Action("sync", f"{trade.trade_date}/{which}: {before or 'unsent'} -> {leg.status}",
                           trade.trade_date)]
        return []

    def _record_pnl(self, trade) -> None:
        entry_px, exit_px = trade.entry.filled_avg_price, trade.exit.filled_avg_price
        qty = min(trade.entry.filled_qty, trade.exit.filled_qty) or trade.entry.filled_qty
        if entry_px and exit_px and qty:
            pnl = (exit_px - entry_px) * qty
            trade.realized_pnl = pnl
            self.state.consecutive_losses = self.state.consecutive_losses + 1 if pnl < 0 else 0
            self._emit("trade_closed", trade_date=trade.trade_date, pnl=pnl, qty=qty,
                       entry=entry_px, exit=exit_px,
                       consecutive_losses=self.state.consecutive_losses)

    def _reference_price(self) -> float:
        """Sizing reference: the freshest price available.

        A live websocket tick, when one is flowing and recent, beats a REST
        round trip -- the share count for the closing-auction order is computed
        from this number. A stale or absent feed falls straight through to the
        broker; a live feed is an accelerator here, never a dependency.
        """
        if self.price_feed is not None:
            price = self.price_feed()
            if price is not None and price > 0:
                return float(price)
        return self.broker.get_last_price(self.settings.symbol)

    def _market_is_open(self, now: dt.datetime, session: Session) -> bool:
        return session.open_dt() <= now < session.close_dt()

    def _last_known_close(self) -> float | None:
        if self.bars is None or self.bars.empty:
            return None
        return float(self.bars["raw_close"].iloc[-1])

    def _data_age_days(self, now: dt.datetime) -> int | None:
        if self.bars is None or self.bars.empty:
            return None
        return (now.date() - self.bars.index[-1].date()).days

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        open_trades = self.state.open_trades()
        closed = [t for t in self.state.trades.values() if t.realized_pnl is not None]
        return {
            "symbol": self.settings.symbol,
            "broker": getattr(self.broker, "name", "?"),
            "paper": self.settings.is_paper,
            "dry_run": self.settings.dry_run,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "open_positions": [
                {"trade_date": t.trade_date, "qty": t.entry.filled_qty, "phase": t.phase.value}
                for t in open_trades
            ],
            "closed_trades": len(closed),
            "realized_pnl": sum(t.realized_pnl or 0.0 for t in closed),
            "consecutive_losses": self.state.consecutive_losses,
            "equity_peak": self.state.equity_peak,
        }
