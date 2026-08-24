"""The run loop.

Deliberately *not* a cron-style scheduler. Because :meth:`OvernightBot.tick` is
idempotent, a plain poll loop is strictly more robust than fire-at-a-time
scheduling: there is no misfire semantics to reason about, a machine that was
asleep across 15:45 simply finds the work still due (or correctly past its
cutoff) on the next tick, and a duplicate tick is a no-op.

The loop sleeps until whichever comes first: the next scheduled decision point
or ``poll_interval_sec``. That keeps it responsive without spinning.
"""

from __future__ import annotations

import datetime as dt
import signal
import threading
import time

from ..calendar_nyse import NY, CalendarError, now_ny
from ..config import Settings
from .audit import get_logger
from .notify import Notifier
from .runner import OvernightBot

log = get_logger("scheduler")

ALERT_KINDS = {"alert", "halt", "skipped", "entry_submitted", "exit_submitted", "entry_filled"}


def next_decision_times(bot: OvernightBot, now: dt.datetime) -> list[dt.datetime]:
    """Upcoming moments at which the bot has something to decide."""
    out: list[dt.datetime] = []
    for offset in (0, 1, 2, 3, 4, 5, 6):
        day = now.date() + dt.timedelta(days=offset)
        try:
            session = bot.calendar.session(day)
        except CalendarError:
            continue
        if session is None:
            continue
        exit_submit, _ = bot.settings.exit_window(session.open_dt())
        entry_submit, entry_cutoff = bot.settings.entry_window(session.close_dt())
        out.extend(
            [
                exit_submit,
                session.open_dt() + dt.timedelta(minutes=bot.settings.verify_after_open_min),
                entry_submit,
                entry_cutoff,
                session.close_dt() + dt.timedelta(minutes=bot.settings.verify_after_close_min),
            ]
        )
        if len(out) >= 10:
            break
    return sorted(t for t in out if t > now)


def sleep_seconds(bot: OvernightBot, now: dt.datetime) -> float:
    """How long to sleep: until the next decision point, capped by the poll interval."""
    upcoming = next_decision_times(bot, now)
    cap = float(bot.settings.poll_interval_sec)
    if not upcoming:
        return cap
    delta = (upcoming[0] - now).total_seconds()
    return max(1.0, min(delta, cap))


def run(
    bot: OvernightBot,
    *,
    settings: Settings | None = None,
    notifier: Notifier | None = None,
    stop: threading.Event | None = None,
    max_iterations: int | None = None,
    sleeper=time.sleep,
    clock=now_ny,
    wake: threading.Event | None = None,
) -> int:
    """Run until stopped. Returns the number of iterations completed.

    ``clock`` and ``sleeper`` are injected so the loop itself is testable
    without waiting in real time. ``wake``, when provided, lets an external
    event source (the trade-updates websocket) cut a sleep short: the loop then
    ticks immediately instead of at the next poll. Because ``tick()`` is
    idempotent, a spurious wake costs one cheap no-op pass -- which is why the
    stream can be wired in with no new state-machine logic at all.
    """
    settings = settings or bot.settings
    notifier = notifier or Notifier(settings.notify_webhook, alert_file=settings.log_dir / "alerts.log")
    stop = stop or threading.Event()

    def _handle(signum, _frame):
        log.info("signal %s received; finishing the current tick and stopping", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):  # not the main thread, or unsupported
            pass

    mode = "DRY RUN" if settings.dry_run else ("PAPER" if settings.is_paper else "*** REAL MONEY ***")
    log.info(
        "starting %s | symbol=%s broker=%s calendar=%s",
        mode, settings.symbol, getattr(bot.broker, "name", "?"), bot.calendar.authority,
    )
    notifier.send("started", f"{mode} | {settings.symbol} via {getattr(bot.broker, 'name', '?')}")

    iterations = 0
    while not stop.is_set():
        now = clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=NY)
        try:
            actions = bot.tick(now)
        except Exception as exc:  # a bug must not silently end the loop
            log.exception("tick failed: %s", exc)
            bot.errors.record(now)
            notifier.alert("tick failed", str(exc))
            actions = []

        for action in actions:
            if action.kind in ALERT_KINDS:
                level = "alert" if action.kind in {"alert", "halt"} else "info"
                notifier.send(action.kind, f"{action.trade_date} {action.detail}", level=level)

        if bot.state.halted:
            notifier.alert("HALTED", bot.state.halt_reason)
            log.error("halted: %s -- human intervention required", bot.state.halt_reason)
            break

        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break
        pause = sleep_seconds(bot, now)
        if wake is not None:
            if wake.wait(timeout=pause):
                wake.clear()
                log.debug("woken early by an order event")
        else:
            sleeper(pause)

    log.info("stopped after %d iterations", iterations)
    return iterations
