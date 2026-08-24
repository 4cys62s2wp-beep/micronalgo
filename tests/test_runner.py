"""The live state machine, driven against a deterministic simulated broker.

Every scenario here is a way the bot can be interrupted, mistimed or lied to.
None of them requires a network.
"""

from __future__ import annotations

import datetime as dt

import pytest

from micronalgo.calendar_nyse import NY, Calendar, ExchangeCalendarsSource
from micronalgo.data.synthetic import random_walk
from micronalgo.live.audit import AuditLog
from micronalgo.live.broker import OrderSide, TimeInForce
from micronalgo.live.reconcile import reconcile
from micronalgo.live.risk import engage_kill_switch
from micronalgo.live.runner import OvernightBot
from micronalgo.live.scheduler import next_decision_times, sleep_seconds
from micronalgo.live.simbroker import SimBroker
from micronalgo.live.state import Phase

TIMES = (dt.time(8, 30), dt.time(9, 35), dt.time(15, 45), dt.time(16, 5))


@pytest.fixture
def rig(tmp_path, settings):
    bars = random_walk(20, seed=42, start="2025-06-02", overnight_mu=0.002,
                       intraday_mu=-0.001, fat_tail_df=None)
    cal = Calendar([ExchangeCalendarsSource()], strict=True)
    broker = SimBroker(bars=bars, cash=100_000.0, calendar=cal)
    bot = OvernightBot(settings, broker, calendar=cal, bars=bars,
                       state_path=tmp_path / "state.json",
                       audit=AuditLog(tmp_path / "audit.jsonl"))
    return bot, broker, bars, cal


def _drive(bot, broker, days, times=TIMES):
    out = []
    for d in days:
        for t in times:
            when = dt.datetime.combine(d, t, tzinfo=NY)
            broker.set_now(when)
            out.extend((d, t, a) for a in bot.tick(when))
    return out


def _sessions(bars, cal, n=None):
    days = [ts.date() for ts in bars.index if cal.is_session(ts.date())]
    return days[:n] if n else days


# ------------------------------------------------------------- the happy path
def test_full_cycle_buys_the_close_and_sells_the_open(rig):
    bot, broker, bars, cal = rig
    days = _sessions(bars, cal, 4)
    _drive(bot, broker, days)

    fills = {(o.side, o.time_in_force) for o in broker.fills}
    assert (OrderSide.BUY, TimeInForce.CLS) in fills
    assert (OrderSide.SELL, TimeInForce.OPG) in fills

    for order in broker.fills:
        day = order.filled_at.date()
        if order.side is OrderSide.BUY:
            assert order.filled_avg_price == pytest.approx(float(bars.loc[str(day), "raw_close"]))
        else:
            assert order.filled_avg_price == pytest.approx(float(bars.loc[str(day), "raw_open"]))


def test_flat_during_the_session(rig):
    bot, broker, bars, cal = rig
    days = _sessions(bars, cal, 5)
    for d in days:
        for t in TIMES:
            when = dt.datetime.combine(d, t, tzinfo=NY)
            broker.set_now(when)
            bot.tick(when)
            if t == dt.time(9, 35):
                pos = broker.get_position("MU")
                assert pos is None or pos.is_flat, f"still holding at {d} {t}"


def test_matches_the_backtest_to_share_rounding(rig):
    from micronalgo.research.costs import scenario
    from micronalgo.research.engine import BacktestConfig, simulate

    bot, broker, bars, cal = rig
    _drive(bot, broker, _sessions(bars, cal))
    sim_equity = broker.get_account().equity

    expected = simulate(bars, BacktestConfig(mode="overnight", cost=scenario("frictionless"),
                                             initial_capital=100_000.0, leverage=0.95))
    assert sim_equity == pytest.approx(float(expected.equity.iloc[-1]), rel=2e-3)


# ------------------------------------------------------------------ restarts
def test_tick_is_idempotent(rig):
    bot, broker, bars, cal = rig
    day = _sessions(bars, cal)[2]
    when = dt.datetime.combine(day, dt.time(15, 45), tzinfo=NY)
    broker.set_now(when)
    for _ in range(20):
        bot.tick(when)
    buys = [o for o in broker._orders.values() if o.side is OrderSide.BUY]
    assert len(buys) == 1, [o.summary() for o in buys]


def test_restart_mid_day_resumes_from_persisted_state(rig, tmp_path, settings):
    bot, broker, bars, cal = rig
    days = _sessions(bars, cal)
    entry_day, exit_day = days[2], days[3]

    when = dt.datetime.combine(entry_day, dt.time(15, 45), tzinfo=NY)
    broker.set_now(when)
    bot.tick(when)
    when = dt.datetime.combine(entry_day, dt.time(16, 5), tzinfo=NY)
    broker.set_now(when)
    bot.tick(when)
    assert bot.state.trade(entry_day).phase is Phase.ENTRY_FILLED

    # A brand new process against the same state file and the same broker.
    fresh = OvernightBot(settings, broker, calendar=cal, bars=bars,
                         state_path=tmp_path / "state.json",
                         audit=AuditLog(tmp_path / "audit2.jsonl"))
    assert fresh.state.trade(entry_day).phase is Phase.ENTRY_FILLED

    when = dt.datetime.combine(exit_day, dt.time(8, 30), tzinfo=NY)
    broker.set_now(when)
    actions = fresh.tick(when)
    assert any(a.kind == "exit_submitted" for a in actions), [str(a) for a in actions]
    sells = [o for o in broker._orders.values() if o.side is OrderSide.SELL]
    assert len(sells) == 1


def test_state_loss_is_recovered_by_reconciliation(rig, tmp_path, settings):
    """The broker is the truth; a wiped journal must not orphan a position."""
    bot, broker, bars, cal = rig
    days = _sessions(bars, cal)
    entry_day = days[2]
    for t in (dt.time(15, 45), dt.time(16, 5)):
        when = dt.datetime.combine(entry_day, t, tzinfo=NY)
        broker.set_now(when)
        bot.tick(when)
    assert broker.get_position("MU").qty > 0

    (tmp_path / "state.json").unlink()
    (tmp_path / "state.json.bak").unlink(missing_ok=True)
    fresh = OvernightBot(settings, broker, calendar=cal, bars=bars,
                         state_path=tmp_path / "state.json",
                         audit=AuditLog(tmp_path / "audit3.jsonl"))
    result = reconcile(broker, fresh.state, symbol="MU")
    # Either the position is adopted, or the bot halts. Silently trading on top
    # of it is the one outcome that must never happen.
    assert result.adopted or fresh.state.halted


# ------------------------------------------------------------- missed windows
def test_missed_entry_window_skips_rather_than_chasing(rig):
    bot, broker, bars, cal = rig
    day = _sessions(bars, cal)[2]
    when = dt.datetime.combine(day, dt.time(15, 55), tzinfo=NY)
    broker.set_now(when)
    actions = bot.tick(when)
    assert any(a.kind == "skipped" for a in actions)
    assert bot.state.trade(day).phase is Phase.SKIPPED
    assert "not chasing" in bot.state.trade(day).skip_reason
    assert not [o for o in broker._orders.values() if o.side is OrderSide.BUY]


def test_missed_exit_window_sends_a_market_order(rig):
    bot, broker, bars, cal = rig
    days = _sessions(bars, cal)
    entry_day, exit_day = days[2], days[3]
    for t in (dt.time(15, 45), dt.time(16, 5)):
        when = dt.datetime.combine(entry_day, t, tzinfo=NY)
        broker.set_now(when)
        bot.tick(when)

    # Wake up well after the open, past the auction cutoff.
    when = dt.datetime.combine(exit_day, dt.time(11, 0), tzinfo=NY)
    broker.set_now(when)
    actions = bot.tick(when)
    assert any(a.kind == "exit_submitted" for a in actions), [str(a) for a in actions]
    sell = next(o for o in broker._orders.values() if o.side is OrderSide.SELL)
    assert sell.time_in_force is TimeInForce.DAY
    assert sell.is_filled
    assert broker.get_position("MU") is None


def test_hold_and_alert_variant_does_not_sell(rig, settings):
    bot, broker, bars, cal = rig
    bot.settings = settings.model_copy(update={"on_missed_exit": "hold_and_alert"})
    days = _sessions(bars, cal)
    for t in (dt.time(15, 45), dt.time(16, 5)):
        when = dt.datetime.combine(days[2], t, tzinfo=NY)
        broker.set_now(when)
        bot.tick(when)
    when = dt.datetime.combine(days[3], dt.time(11, 0), tzinfo=NY)
    broker.set_now(when)
    actions = bot.tick(when)
    assert any(a.kind == "alert" for a in actions)
    assert broker.get_position("MU").qty > 0


# ------------------------------------------------------------------ failures
def test_rejected_entry_does_not_leave_a_phantom_position(rig):
    bot, broker, bars, cal = rig
    broker.reject_reason = "insufficient buying power"
    day = _sessions(bars, cal)[2]
    for t in (dt.time(15, 45), dt.time(16, 5)):
        when = dt.datetime.combine(day, t, tzinfo=NY)
        broker.set_now(when)
        bot.tick(when)
    assert bot.state.trade(day).phase is Phase.ENTRY_FAILED
    assert broker.get_position("MU") is None


def test_dead_exit_order_escalates_to_a_market_order(rig):
    bot, broker, bars, cal = rig
    days = _sessions(bars, cal)
    entry_day, exit_day = days[2], days[3]
    for t in (dt.time(15, 45), dt.time(16, 5)):
        when = dt.datetime.combine(entry_day, t, tzinfo=NY)
        broker.set_now(when)
        bot.tick(when)

    when = dt.datetime.combine(exit_day, dt.time(8, 30), tzinfo=NY)
    broker.set_now(when)
    bot.tick(when)
    trade = bot.state.trade(entry_day)
    # Kill the resting auction order behind the bot's back.
    broker.cancel_order(trade.exit.broker_order_id)

    when = dt.datetime.combine(exit_day, dt.time(9, 40), tzinfo=NY)
    broker.set_now(when)
    actions = bot.tick(when)
    assert any(a.kind == "exit_escalated" for a in actions), [str(a) for a in actions]
    assert broker.get_position("MU") is None
    assert trade.exit.attempt == 1


def test_exit_size_follows_the_broker_not_the_journal(rig):
    """Overselling a partially filled entry would leave a naked short."""
    bot, broker, bars, cal = rig
    days = _sessions(bars, cal)
    entry_day, exit_day = days[2], days[3]
    for t in (dt.time(15, 45), dt.time(16, 5)):
        when = dt.datetime.combine(entry_day, t, tzinfo=NY)
        broker.set_now(when)
        bot.tick(when)

    trade = bot.state.trade(entry_day)
    trade.entry.filled_qty += 500  # journal now overstates the position

    when = dt.datetime.combine(exit_day, dt.time(8, 30), tzinfo=NY)
    broker.set_now(when)
    bot.tick(when)
    sell = next(o for o in broker._orders.values() if o.side is OrderSide.SELL)
    assert sell.qty == broker.get_position("MU").qty


# --------------------------------------------------------------- risk / halts
def test_kill_switch_blocks_entries_but_never_exits(rig, settings):
    """A kill switch that also blocks exits would strand a position overnight,
    in the exposure this strategy exists to avoid."""
    bot, broker, bars, cal = rig
    days = _sessions(bars, cal)
    entry_day, exit_day = days[2], days[3]
    for t in (dt.time(15, 45), dt.time(16, 5)):
        when = dt.datetime.combine(entry_day, t, tzinfo=NY)
        broker.set_now(when)
        bot.tick(when)
    assert broker.get_position("MU").qty > 0

    engage_kill_switch(settings.kill_switch_file, "test")

    when = dt.datetime.combine(exit_day, dt.time(8, 30), tzinfo=NY)
    broker.set_now(when)
    actions = bot.tick(when)
    assert any(a.kind == "exit_submitted" for a in actions), [str(a) for a in actions]

    when = dt.datetime.combine(exit_day, dt.time(15, 45), tzinfo=NY)
    broker.set_now(when)
    actions = bot.tick(when)
    assert any(a.kind == "skipped" and "kill switch" in a.detail for a in actions)


def test_dry_run_sends_nothing(rig, settings):
    bot, broker, bars, cal = rig
    bot.settings = settings.model_copy(update={"dry_run": True})
    days = _sessions(bars, cal, 4)
    actions = _drive(bot, broker, days)
    assert any(a.kind == "dry_run" for _, _, a in actions)
    assert broker._orders == {}


def test_non_session_day_is_a_no_op(rig):
    bot, broker, bars, cal = rig
    saturday = dt.date(2025, 6, 7)
    when = dt.datetime.combine(saturday, dt.time(15, 45), tzinfo=NY)
    broker.set_now(when)
    actions = bot.tick(when)
    assert [a.kind for a in actions] == ["idle"]


# ----------------------------------------------------------------- scheduling
def test_half_day_shifts_the_decision_points(rig):
    bot, _, _, _ = rig
    now = dt.datetime(2025, 11, 26, 10, 0, tzinfo=NY)
    points = next_decision_times(bot, now)
    friday = [t for t in points if t.date() == dt.date(2025, 11, 28)]
    # Thanksgiving (27th) produces nothing; the 28th closes at 13:00.
    assert not [t for t in points if t.date() == dt.date(2025, 11, 27)]
    assert (dt.time(12, 45) in [t.time() for t in friday])
    assert sleep_seconds(bot, now) <= bot.settings.poll_interval_sec
