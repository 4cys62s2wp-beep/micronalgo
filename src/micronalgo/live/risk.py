"""Risk guards.

Design rule: **every guard is a veto, none is a trigger.** Nothing in this module
can cause a trade; it can only prevent one, or force a flat. That asymmetry is
deliberate -- a bug in a veto costs a missed trade, a bug in a trigger costs a
position nobody asked for.

Guards are evaluated fresh at each decision point and the reasons are recorded
verbatim in the audit log, so "why did it not trade last Tuesday" is always a
one-line answer rather than an investigation.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from .broker import Account
from .state import BotState


@dataclass
class RiskContext:
    account: Account | None = None
    reference_price: float | None = None
    last_known_close: float | None = None
    data_age_days: int | None = None
    asset_tradable: bool | None = None
    intended_notional: float = 0.0
    intended_shares: float = 0.0
    now: dt.datetime | None = None


@dataclass
class RiskVerdict:
    allow: bool
    blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.blocks) if self.blocks else "ok"

    def __bool__(self) -> bool:
        return self.allow


class ErrorBudget:
    """Circuit breaker over a sliding window of API failures.

    A broker that is throwing errors is a broker whose reported positions you
    cannot trust. Past the budget the bot stops initiating anything and asks for
    a human, rather than acting on a picture that may be stale.
    """

    def __init__(self, limit: int, window_minutes: int) -> None:
        self.limit = limit
        self.window = dt.timedelta(minutes=window_minutes)
        self._events: deque[dt.datetime] = deque()

    def record(self, when: dt.datetime | None = None) -> None:
        self._events.append(when or dt.datetime.now(dt.timezone.utc))
        self._trim(self._events[-1])

    def _trim(self, now: dt.datetime) -> None:
        while self._events and (now - self._events[0]) > self.window:
            self._events.popleft()

    def count(self, now: dt.datetime | None = None) -> int:
        now = now or dt.datetime.now(dt.timezone.utc)
        self._trim(now)
        return len(self._events)

    def tripped(self, now: dt.datetime | None = None) -> bool:
        return self.count(now) >= self.limit

    def reset(self) -> None:
        self._events.clear()


def kill_switch_active(path: Path | str) -> bool:
    """A file on disk that stops trading. Deliberately the crudest possible
    mechanism: it works when the config is broken, the API is down, and the
    person reaching for it is panicking."""
    return Path(path).exists()


def engage_kill_switch(path: Path | str, reason: str = "manual") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {reason}\n", encoding="utf-8")
    return p


def release_kill_switch(path: Path | str) -> bool:
    p = Path(path)
    if p.exists():
        p.unlink()
        return True
    return False


def evaluate(
    settings: Settings,
    state: BotState,
    ctx: RiskContext,
    *,
    error_budget: ErrorBudget | None = None,
) -> RiskVerdict:
    """Run every guard. Returns the full list of blocks, not just the first."""
    blocks: list[str] = []
    warnings: list[str] = []

    if kill_switch_active(settings.kill_switch_file):
        blocks.append(f"kill switch present at {settings.kill_switch_file}")

    if state.halted:
        blocks.append(f"bot halted: {state.halt_reason}")

    if error_budget is not None and error_budget.tripped(ctx.now):
        blocks.append(
            f"API error budget exhausted ({error_budget.count(ctx.now)} errors in "
            f"{settings.api_error_window_min}m); broker state is not trustworthy"
        )

    acct = ctx.account
    if acct is not None:
        if acct.blocked:
            blocks.append("broker reports the account is blocked from trading")
        if acct.equity <= 0:
            blocks.append("account equity is zero or negative")

        peak = max(state.equity_peak, acct.equity)
        if peak > 0:
            dd = acct.equity / peak - 1.0
            if dd <= -abs(settings.max_drawdown_halt):
                blocks.append(
                    f"drawdown {dd:.2%} breaches the halt threshold "
                    f"{-abs(settings.max_drawdown_halt):.2%} (peak ${peak:,.0f})"
                )
            elif dd <= -abs(settings.max_drawdown_halt) * 0.6:
                warnings.append(f"drawdown {dd:.2%} approaching the halt threshold")

        if acct.last_equity > 0:
            day = acct.equity / acct.last_equity - 1.0
            if day <= -abs(settings.daily_loss_limit):
                blocks.append(
                    f"daily loss {day:.2%} breaches the limit {-abs(settings.daily_loss_limit):.2%}"
                )

        if ctx.intended_notional > acct.buying_power:
            blocks.append(
                f"intended notional ${ctx.intended_notional:,.0f} exceeds buying power ${acct.buying_power:,.0f}"
            )

    if state.consecutive_losses >= settings.max_consecutive_losses:
        blocks.append(
            f"{state.consecutive_losses} consecutive losing trades "
            f"(limit {settings.max_consecutive_losses}) -- the edge may have stopped working"
        )

    if ctx.asset_tradable is False:
        blocks.append("broker reports the asset is not tradable (halt, delisting or restriction)")

    if ctx.reference_price is not None and ctx.last_known_close:
        dev = abs(ctx.reference_price / ctx.last_known_close - 1.0)
        if dev > settings.max_price_deviation:
            blocks.append(
                f"reference price {ctx.reference_price:.2f} deviates {dev:.1%} from the last known close "
                f"{ctx.last_known_close:.2f} (limit {settings.max_price_deviation:.0%}); "
                "this is what a bad quote or an unapplied split looks like"
            )

    if ctx.data_age_days is not None and ctx.data_age_days > settings.max_data_age_days:
        blocks.append(f"price history is {ctx.data_age_days} days stale (limit {settings.max_data_age_days})")

    if ctx.intended_notional > settings.max_notional:
        blocks.append(
            f"intended notional ${ctx.intended_notional:,.0f} exceeds the configured cap "
            f"${settings.max_notional:,.0f}"
        )
    if ctx.intended_shares > settings.max_shares:
        blocks.append(f"intended {ctx.intended_shares:,.0f} shares exceeds the cap {settings.max_shares:,}")

    if ctx.reference_price is not None and ctx.reference_price <= 0:
        blocks.append("reference price is not positive")

    return RiskVerdict(allow=not blocks, blocks=blocks, warnings=warnings)
