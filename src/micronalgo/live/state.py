"""Persistent trading state.

Durability rules
----------------
* Writes are **atomic**: a temp file in the same directory plus ``os.replace``.
  A crash mid-write leaves either the old file or the new one, never a truncated
  one. A ``.bak`` copy of the last good state is kept for the case where the
  file is damaged by something outside this process.
* The state is a **cache of broker truth, never the source of it**. On start,
  :mod:`micronalgo.live.reconcile` compares it against the broker's actual
  positions and orders, and the broker wins every disagreement.
* Schema changes go through ``version`` and an explicit migration, because a
  state file that silently loses a field is a position nobody is tracking.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

STATE_VERSION = 1


class StateVersionError(RuntimeError):
    """State file was written by a newer version of this package."""


class Phase(str, Enum):
    """Lifecycle of one overnight trade, keyed by its entry session date."""

    PENDING = "pending"            # not yet decided
    SKIPPED = "skipped"            # a filter or a risk guard said no
    ENTRY_SUBMITTED = "entry_submitted"
    ENTRY_FILLED = "entry_filled"
    ENTRY_FAILED = "entry_failed"  # rejected / expired / never filled
    EXIT_SUBMITTED = "exit_submitted"
    EXIT_FILLED = "exit_filled"
    EXIT_FAILED = "exit_failed"    # still holding: needs human attention
    CLOSED = "closed"

    @property
    def holds_position(self) -> bool:
        return self in {Phase.ENTRY_FILLED, Phase.EXIT_SUBMITTED, Phase.EXIT_FAILED}

    @property
    def is_done(self) -> bool:
        return self in {Phase.SKIPPED, Phase.ENTRY_FAILED, Phase.CLOSED}


@dataclass
class LegState:
    client_order_id: str = ""
    broker_order_id: str = ""
    attempt: int = 0
    qty: float = 0.0
    status: str = ""
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    submitted_at: str = ""
    filled_at: str = ""
    note: str = ""


@dataclass
class TradeState:
    trade_date: str
    phase: Phase = Phase.PENDING
    entry: LegState = field(default_factory=LegState)
    exit: LegState = field(default_factory=LegState)
    skip_reason: str = ""
    intended_qty: float = 0.0
    reference_price: float | None = None
    realized_pnl: float | None = None
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
        self.notes.append(f"{stamp} {text}")
        del self.notes[:-40]


@dataclass
class BotState:
    version: int = STATE_VERSION
    symbol: str = "MU"
    trades: dict[str, TradeState] = field(default_factory=dict)
    equity_peak: float = 0.0
    last_equity: float = 0.0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    started_at: str = ""
    updated_at: str = ""

    # ------------------------------------------------------------------ #
    def trade(self, trade_date: dt.date | str, *, create: bool = True) -> TradeState:
        key = trade_date.isoformat() if isinstance(trade_date, dt.date) else str(trade_date)
        if key not in self.trades:
            if not create:
                raise KeyError(key)
            self.trades[key] = TradeState(trade_date=key)
        return self.trades[key]

    def open_trades(self) -> list[TradeState]:
        """Trades that are, or believe they are, holding a position."""
        return [t for t in self.trades.values() if t.phase.holds_position]

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""

    def prune(self, keep: int = 400) -> None:
        """Drop old *finished* trades; anything unresolved is kept forever."""
        finished = sorted(k for k, t in self.trades.items() if t.phase.is_done)
        for key in finished[: max(0, len(finished) - keep)]:
            del self.trades[key]


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def _to_jsonable(state: BotState) -> dict[str, Any]:
    payload = asdict(state)
    payload["trades"] = {
        k: {**asdict(v), "phase": v.phase.value} for k, v in state.trades.items()
    }
    payload["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return payload


def _from_jsonable(payload: dict[str, Any]) -> BotState:
    version = int(payload.get("version", 0))
    if version > STATE_VERSION:
        raise StateVersionError(
            f"state file was written by a newer version ({version} > {STATE_VERSION}); refusing to downgrade"
        )
    trades: dict[str, TradeState] = {}
    for key, raw in (payload.get("trades") or {}).items():
        raw = dict(raw)
        entry = LegState(**(raw.pop("entry", {}) or {}))
        exit_ = LegState(**(raw.pop("exit", {}) or {}))
        raw["phase"] = Phase(raw.get("phase", Phase.PENDING.value))
        raw.pop("trade_date", None)
        trades[key] = TradeState(trade_date=key, entry=entry, exit=exit_, **raw)

    known = {"version", "symbol", "equity_peak", "last_equity", "consecutive_losses",
             "halted", "halt_reason", "started_at", "updated_at"}
    kwargs = {k: v for k, v in payload.items() if k in known}
    kwargs["version"] = STATE_VERSION
    return BotState(trades=trades, **kwargs)


def save(state: BotState, path: Path | str) -> Path:
    """Atomically persist ``state``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(_to_jsonable(state), indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load(path: Path | str, *, symbol: str = "MU") -> BotState:
    """Load state, falling back to the backup, then to a fresh state."""
    path = Path(path)
    damaged: list[str] = []
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            return _from_jsonable(json.loads(candidate.read_text(encoding="utf-8")))
        except StateVersionError:
            # A newer schema is a deliberate stop, not corruption: silently
            # starting fresh here would abandon a live position.
            raise
        except Exception as exc:
            damaged.append(f"{candidate.name}: {type(exc).__name__}: {exc}")
            continue

    if damaged:
        # Both copies unreadable. Start fresh, but make the loss impossible to
        # miss -- reconciliation against the broker is what recovers the truth.
        quarantine = path.with_suffix(path.suffix + ".corrupt")
        try:
            if path.exists():
                shutil.copy2(path, quarantine)
        except OSError:
            pass
        import warnings
        warnings.warn(
            "state file unreadable, starting from empty state; broker reconciliation "
            f"must resolve any open position. Details: {'; '.join(damaged)}",
            RuntimeWarning,
            stacklevel=2,
        )
    return BotState(
        symbol=symbol,
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )


class InstanceLock:
    """Advisory file lock preventing two bots from sharing one state directory.

    Cron overlap and "I'll just run it manually too" are the realistic ways to
    end up with two processes. The deterministic ``client_order_id`` already
    stops them from placing two orders for the same leg -- the venue rejects the
    duplicate -- but they would still race on the state file and each write a
    half-true picture. This makes the second process exit immediately and say
    which pid holds the lock.

    Advisory, and released when the process dies, so a crash never leaves the
    system wedged. On platforms without ``fcntl`` it degrades to a no-op rather
    than blocking use.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.seek(0)
            holder = self._fh.read().strip() or "unknown"
            self._fh.close()
            self._fh = None
            raise RuntimeError(
                f"another micronalgo instance holds {self.path} (pid {holder}). "
                "Two bots sharing one state directory will disagree about the position. "
                "If the LaunchAgent is loaded, this is most likely a manual "
                "`micronalgo paper` still running in a terminal -- stop that one, or "
                "unload the agent with `launchctl unload "
                "~/Library/LaunchAgents/com.micronalgo.paper.plist`."
            ) from None
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
