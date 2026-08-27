"""Real-time layer: parsers, price cache, wake integration.

No network anywhere; frames are recorded payload shapes and the worker is
driven synchronously.
"""

from __future__ import annotations

import datetime as dt
import json
import threading

import pytest

from micronalgo.calendar_nyse import NY, Calendar, ExchangeCalendarsSource
from micronalgo.data.synthetic import random_walk
from micronalgo.live.audit import AuditLog
from micronalgo.live.runner import OvernightBot
from micronalgo.live.scheduler import run
from micronalgo.live.simbroker import SimBroker
from micronalgo.live.stream import (
    LivePrice,
    StreamWorker,
    TradeTick,
    parse_market_message,
    parse_trade_update,
)


# ------------------------------------------------------------------ parsers
def test_market_parser_extracts_trades_and_survives_noise():
    frames = [
        json.dumps([{"T": "success", "msg": "authenticated"}]),
        json.dumps([{"T": "subscription", "trades": ["MU"]}]),
        json.dumps([{"T": "t", "S": "MU", "p": 98.75, "s": 100, "t": "2026-08-24T19:59:58Z"}]),
        json.dumps([{"T": "t", "S": "MU", "p": -1.0, "s": 100}]),          # bad price
        json.dumps([{"T": "error", "code": 406, "msg": "limit"}]),
        json.dumps({"T": "t", "S": "MU", "p": 99.10, "s": 5}),             # dict, not list
        "garbage",
        json.dumps([{"unexpected": "shape"}]),
    ]
    ticks = [t for f in frames for t in parse_market_message(f)]
    assert [(t.symbol, t.price) for t in ticks] == [("MU", 98.75), ("MU", 99.10)]


@pytest.mark.parametrize("shape", ["legacy", "flat"])
def test_trade_update_parser_handles_both_endpoint_generations(shape):
    order = {"client_order_id": "mna-MU-2026-08-24-BUY-0", "symbol": "MU",
             "filled_qty": "250", "filled_avg_price": "98.735", "status": "filled"}
    if shape == "legacy":
        frame = json.dumps({"stream": "trade_updates", "data": {"event": "fill", "order": order}})
    else:
        frame = json.dumps({"event": "fill", "order": order})
    e = parse_trade_update(frame)
    assert e is not None and e.event == "fill" and e.filled_qty == 250.0
    assert e.is_actionable


def test_control_frames_and_noise_return_none():
    for frame in (
        json.dumps({"stream": "authorization", "data": {"status": "authorized"}}),
        json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}),
        json.dumps({"event": "new"}),                      # no order object
        "not json",
        json.dumps([1, 2, 3]),
    ):
        assert parse_trade_update(frame) is None


def test_new_order_event_is_not_actionable():
    frame = json.dumps({"event": "new", "order": {"client_order_id": "x", "symbol": "MU",
                                                  "filled_qty": "0", "status": "new"}})
    e = parse_trade_update(frame)
    assert e is not None and not e.is_actionable


# ------------------------------------------------------------------ price cache
def test_live_price_expires():
    clock = [100.0]
    mono = lambda: clock[0]  # noqa: E731
    lp = LivePrice()
    assert lp.get(monotonic=mono) is None
    lp.update(TradeTick("MU", 98.75, 100, dt.datetime.now(dt.timezone.utc)), monotonic=mono)
    assert lp.get(30, monotonic=mono) == 98.75
    clock[0] += 31
    assert lp.get(30, monotonic=mono) is None, "a stale price must fall back to REST, not be served"


# ------------------------------------------------------------------ worker
def test_worker_feeds_messages_and_counts_them():
    seen: list[str] = []
    w = StreamWorker("test", connect=lambda: None, on_message=seen.append)
    w.drive(["a", "b", "c"])
    assert seen == ["a", "b", "c"] and w.n_messages == 3


# ------------------------------------------------------------------ integration
@pytest.fixture
def rig(tmp_path, settings):
    bars = random_walk(20, seed=42, start="2025-06-02", fat_tail_df=None)
    cal = Calendar([ExchangeCalendarsSource()], strict=True)
    broker = SimBroker(bars=bars, cash=100_000.0, calendar=cal)
    return settings, broker, bars, cal, tmp_path


def test_bot_prefers_a_fresh_feed_price_and_falls_back_when_stale(rig):
    settings, broker, bars, cal, tmp_path = rig
    feed_value: list[float | None] = [None]
    bot = OvernightBot(settings, broker, calendar=cal, bars=bars,
                       state_path=tmp_path / "s.json", audit=AuditLog(tmp_path / "a.jsonl"),
                       price_feed=lambda: feed_value[0])
    day = bars.index[3].date()
    broker.set_now(dt.datetime.combine(day, dt.time(15, 45), tzinfo=NY))

    rest_price = broker.get_last_price("MU")
    assert bot._reference_price() == pytest.approx(rest_price)   # feed empty -> REST

    feed_value[0] = rest_price * 1.001
    assert bot._reference_price() == pytest.approx(rest_price * 1.001)  # fresh feed wins

    feed_value[0] = -5.0
    assert bot._reference_price() == pytest.approx(rest_price)   # nonsense -> REST


def test_wake_event_cuts_the_sleep_short(rig):
    """An order event must trigger an immediate tick, not wait out the poll."""
    settings, broker, bars, cal, tmp_path = rig
    bot = OvernightBot(settings, broker, calendar=cal, bars=bars,
                       state_path=tmp_path / "s.json", audit=AuditLog(tmp_path / "a.jsonl"))
    day = bars.index[3].date()
    broker.set_now(dt.datetime.combine(day, dt.time(10, 0), tzinfo=NY))

    wake = threading.Event()
    wake.set()  # an event is already pending when the loop reaches its sleep
    slept: list[float] = []
    run(bot, settings=settings, max_iterations=2, wake=wake,
        clock=lambda: dt.datetime.combine(day, dt.time(10, 0), tzinfo=NY),
        sleeper=slept.append)
    # The wake path consumed the pending event instead of sleeping.
    assert not wake.is_set()
    assert slept == []


def test_spurious_wakes_are_harmless(rig):
    """tick() is idempotent, so waking fifty times must submit nothing extra."""
    settings, broker, bars, cal, tmp_path = rig
    bot = OvernightBot(settings, broker, calendar=cal, bars=bars,
                       state_path=tmp_path / "s.json", audit=AuditLog(tmp_path / "a.jsonl"))
    day = bars.index[3].date()
    when = dt.datetime.combine(day, dt.time(15, 45), tzinfo=NY)
    broker.set_now(when)
    for _ in range(50):
        bot.tick(when)
    buys = list(broker._orders.values())
    assert len(buys) == 1


def test_pinging_socket_pings_on_idle_instead_of_reconnecting():
    """A quiet stream must not be mistaken for a dead one."""
    from micronalgo.live.stream import _PingingSocket

    class Timeout(Exception):
        pass

    class FakeSock:
        def __init__(self):
            self.pings = 0
            self.sequence = [Timeout(), Timeout(), "frame-1"]

        def recv(self):
            item = self.sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def ping(self):
            self.pings += 1

    inner = FakeSock()
    sock = _PingingSocket(inner, Timeout)
    assert sock.recv() == "frame-1"
    assert inner.pings == 2, "each idle timeout must ping, not reconnect"


def test_pinging_socket_escalates_when_the_peer_is_gone():
    from micronalgo.live.stream import _PingingSocket

    class Timeout(Exception):
        pass

    class DeadSock:
        def recv(self):
            raise Timeout()

        def ping(self):
            raise ConnectionError("peer gone")

    with pytest.raises(ConnectionError):
        _PingingSocket(DeadSock(), Timeout).recv()
