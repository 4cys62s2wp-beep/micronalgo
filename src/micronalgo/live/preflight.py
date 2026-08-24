"""Preflight: verify against the real API what the build could only assume.

This package was written in an environment with no route to any broker or
market-data host. Everything that could be pinned down offline was pinned down
by unit tests over recorded payloads. What remains is a list of runtime facts,
each marked ``[verify-at-runtime]`` in the source, and this module checks every
one of them against the user's actual paper account before a single real order
is ever placed.

Run it first. If it does not come back clean, do not start the bot.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from ..calendar_nyse import Calendar, CalendarError, now_ny
from ..config import Settings
from .broker import (
    Broker,
    BrokerError,
    OrderSide,
    OrderType,
    TimeInForce,
    make_client_order_id,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    critical: bool = True

    def __str__(self) -> str:
        mark = "PASS" if self.ok else ("FAIL" if self.critical else "WARN")
        return f"[{mark}] {self.name}: {self.detail}"


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, *, critical: bool = True) -> None:
        self.checks.append(Check(name, ok, detail, critical))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.critical]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.critical]

    @property
    def ok(self) -> bool:
        return not self.failures

    def render(self) -> str:
        lines = [str(c) for c in self.checks]
        lines.append("")
        lines.append(
            f"{len(self.checks) - len(self.failures) - len(self.warnings)} passed, "
            f"{len(self.warnings)} warnings, {len(self.failures)} failures"
        )
        if not self.ok:
            lines.append("PREFLIGHT FAILED -- do not start the bot until these are resolved.")
        return "\n".join(lines)


def run_preflight(
    settings: Settings,
    broker: Broker,
    calendar: Calendar,
    *,
    probe_orders: bool = False,
    now: dt.datetime | None = None,
) -> PreflightReport:
    """Check every runtime assumption. ``probe_orders`` places and cancels a
    one-share auction order to prove the venue accepts the order types this
    strategy depends on -- allowed on a paper account only."""
    report = PreflightReport()
    now = now or now_ny()

    # 1. Credentials and account -------------------------------------------------
    try:
        account = broker.get_account()
        report.add(
            "account", not account.blocked,
            f"equity ${account.equity:,.2f}, buying power ${account.buying_power:,.2f}, "
            f"blocked={account.blocked}, multiplier={account.multiplier:g}",
        )
    except BrokerError as exc:
        report.add("account", False, f"cannot read the account: {exc}")
        return report

    # 2. Paper vs real -----------------------------------------------------------
    report.add(
        "paper_endpoint", settings.is_paper,
        f"broker={settings.broker} base_url={settings.alpaca_base_url} -> "
        + ("paper (safe)" if settings.is_paper else "REAL MONEY"),
        critical=False,
    )

    # 3. Clock and calendar ------------------------------------------------------
    try:
        clock = broker.get_clock()
        report.add("clock", True, f"broker time {clock.timestamp.isoformat()}, market open={clock.is_open}")
    except BrokerError as exc:
        report.add("clock", False, f"cannot read the broker clock: {exc}")

    try:
        start = now.date()
        end = start + dt.timedelta(days=90)
        broker_cal = broker.get_calendar(start, end)
        report.add("broker_calendar", bool(broker_cal), f"{len(broker_cal)} sessions in the next 90 days")

        mismatches: list[str] = []
        early_days = 0
        for day, (b_open, b_close) in sorted(broker_cal.items()):
            if b_close < dt.time(16, 0):
                early_days += 1
            try:
                local = calendar.session(day)
            except CalendarError as exc:
                mismatches.append(f"{day}: local calendar unresolved ({exc})")
                continue
            if local is None:
                mismatches.append(f"{day}: broker says open, local calendar says closed")
            elif (local.open_time, local.close_time) != (b_open, b_close):
                mismatches.append(
                    f"{day}: broker {b_open}-{b_close} vs local {local.open_time}-{local.close_time}"
                )
        report.add(
            "calendar_agreement", not mismatches,
            "local calendar matches the broker" if not mismatches
            else f"{len(mismatches)} disagreements: {mismatches[:3]}",
        )
        report.add(
            "early_closes", True,
            f"{early_days} early closes in the window; the entry window shifts with the session close",
            critical=False,
        )
    except BrokerError as exc:
        report.add("broker_calendar", False, f"cannot read the broker calendar: {exc}")

    # 4. Instrument --------------------------------------------------------------
    try:
        tradable = broker.is_tradable(settings.symbol)
        report.add("asset_tradable", tradable, f"{settings.symbol} tradable={tradable}")
    except BrokerError as exc:
        report.add("asset_tradable", False, f"cannot read the asset: {exc}")

    frac = getattr(broker, "supports_fractional", None)
    if callable(frac):
        try:
            supported = frac(settings.symbol)
            consistent = supported or not settings.fractional_shares
            report.add(
                "fractional_shares", consistent,
                f"venue fractionable={supported}, configured fractional_shares={settings.fractional_shares}"
                + ("" if consistent else " -- auction orders normally require whole shares"),
                critical=False,
            )
        except BrokerError:
            pass

    try:
        price = broker.get_last_price(settings.symbol)
        report.add("market_data", price > 0, f"last price {settings.symbol} = {price:.4f}")
    except BrokerError as exc:
        report.add("market_data", False, f"no market data: {exc}")

    # 5. Local environment -------------------------------------------------------
    try:
        settings.ensure_dirs()
        probe = Path(settings.state_dir) / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add("state_writable", True, f"{settings.state_dir} is writable")
    except OSError as exc:
        report.add("state_writable", False, f"cannot write to {settings.state_dir}: {exc}")

    report.add(
        "kill_switch", not Path(settings.kill_switch_file).exists(),
        f"{settings.kill_switch_file} " + ("absent (trading enabled)"
                                           if not Path(settings.kill_switch_file).exists()
                                           else "PRESENT -- trading is disabled"),
        critical=False,
    )
    report.add(
        "dry_run", True,
        "dry_run is ON: orders will be logged, not sent" if settings.dry_run
        else "dry_run is OFF: orders WILL be sent",
        critical=False,
    )

    # 6. Order-type support (the assumption everything else rests on) ------------
    if probe_orders:
        report.checks.extend(_probe_order_types(settings, broker, calendar, now))
    else:
        report.add(
            "order_types", True,
            "not probed. Auction order support (time_in_force 'cls' and 'opg') is the single "
            "assumption this strategy depends on -- re-run with --probe-orders to verify it.",
            critical=False,
        )

    return report


def _probe_order_types(settings: Settings, broker: Broker, calendar: Calendar,
                       now: dt.datetime) -> list[Check]:
    """Submit and immediately cancel a 1-share auction order of each type.

    Paper accounts only. A closing-auction order cannot be cancelled once inside
    the venue's cutoff, so the probe refuses to run near the close rather than
    leaving an unwanted order live.
    """
    checks: list[Check] = []
    if not settings.is_paper:
        return [Check("order_types", False, "refusing to probe order types on a non-paper account")]

    try:
        session = calendar.session(now.date())
    except CalendarError as exc:
        return [Check("order_types", False, f"calendar unresolved: {exc}", critical=False)]

    if session is not None:
        _, entry_cutoff = settings.entry_window(session.close_dt())
        # Inclusive: at exactly cutoff-10m the probe is already unsafe, and the
        # safe direction on a boundary is to refuse.
        if now >= entry_cutoff - dt.timedelta(minutes=10):
            return [Check(
                "order_types", False,
                "too close to the closing auction to probe safely (a 'cls' order could not be "
                "cancelled); run the probe earlier in the day",
                critical=False,
            )]

    for tif, label in ((TimeInForce.CLS, "market-on-close"), (TimeInForce.OPG, "market-on-open")):
        cid = make_client_order_id(
            "probe", settings.symbol, now.date(), tif.value, int(now.timestamp()) % 1000
        )
        order = None
        try:
            order = broker.submit_order(
                symbol=settings.symbol, side=OrderSide.BUY, qty=1,
                order_type=OrderType.MARKET, time_in_force=tif, client_order_id=cid,
            )
            accepted = order.status.value not in {"rejected"}
            checks.append(Check(
                f"order_type_{tif.value}", accepted,
                f"{label} (time_in_force={tif.value!r}) -> {order.status.value}",
            ))
        except BrokerError as exc:
            checks.append(Check(
                f"order_type_{tif.value}", False,
                f"{label} (time_in_force={tif.value!r}) rejected: {exc}",
            ))
        finally:
            if order is not None and order.broker_order_id:
                try:
                    broker.cancel_order(order.broker_order_id)
                except BrokerError as exc:
                    checks.append(Check(
                        f"order_cancel_{tif.value}", False,
                        f"PROBE ORDER LEFT LIVE -- cancel it manually: {order.broker_order_id} ({exc})",
                    ))
    return checks
