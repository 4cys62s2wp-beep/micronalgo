"""Real-time layer: Alpaca websocket streams.

What real time buys this strategy, stated honestly
--------------------------------------------------
The strategy transacts twice a day, at auction prints that every participant
receives at the same price. Millisecond reaction speed buys **no edge on the
fills themselves** -- an MOC order submitted at 15:45:00 and one submitted at
15:49:00 fill at the identical closing print. What streaming genuinely improves
is *operations*:

* **Fill confirmation in seconds, not minutes.** The trade-updates stream pushes
  order events the moment they happen, so a rejected or expired exit order is
  escalated within one tick instead of at the ``open + 5m`` verification pass.
  The failure mode that matters -- accidentally holding through the intraday
  window -- gets minutes shaved off it.
* **A fresh reference price for sizing.** The share count for the closing-auction
  buy is computed from the last trade. A live trade feed keeps that reference
  seconds old instead of one REST round trip old.
* **A live view inside the paper process.** The running bot sizes and sanity-
  checks against the streamed price. (``status --watch`` is a separate process
  reading state files; it deliberately has no socket of its own.)

Because of that, the design principle is: **the stream is an accelerator, never
a dependency.** Every message merely wakes the idempotent ``tick()`` early or
refreshes a cached price. If the socket dies, nothing is lost -- the poll loop
was running anyway, and reconnection happens with capped backoff in the
background. The bot must behave identically (only slower) with streaming off.

Testing without a network
-------------------------
The build environment cannot reach any Alpaca host, so the split is the same as
in :mod:`micronalgo.live.alpaca`: message *parsing* is pure functions over
recorded payload shapes, unit-tested offline; the socket lifecycle is a worker
class whose connection factory is injectable, driven by a fake in tests. The
exact endpoint URLs and auth message shapes are [verify-at-runtime] -- the
``paper`` command logs precisely what the stream is doing at startup, and
degrades to polling with a visible log line if the handshake fails.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .audit import get_logger

log = get_logger("stream")

MARKET_DATA_URL = "wss://stream.data.alpaca.markets/v2/{feed}"   # [verify-at-runtime]
TRADE_UPDATES_URL = "wss://paper-api.alpaca.markets/stream"      # [verify-at-runtime]

RECONNECT_BASE_S = 2.0
RECONNECT_MAX_S = 60.0


# --------------------------------------------------------------------------- #
# Pure parsers -- unit-tested offline against recorded payload shapes.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TradeTick:
    symbol: str
    price: float
    size: float
    at: dt.datetime


@dataclass(frozen=True)
class OrderEvent:
    event: str              # fill / partial_fill / canceled / rejected / expired / new ...
    client_order_id: str
    symbol: str
    filled_qty: float
    filled_avg_price: float | None
    status: str
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        """Events after which the bot should look at the order immediately."""
        return self.event in {
            "fill", "partial_fill", "canceled", "rejected", "expired",
            "done_for_day", "replaced", "order_cancel_rejected",
        }


def _ts(value: Any) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return dt.datetime.now(dt.timezone.utc)


def parse_market_message(raw: str | bytes) -> list[TradeTick]:
    """Parse one frame from the market-data stream into trade ticks.

    The v2 stream sends a JSON *array* of messages, each tagged with ``"T"``:
    ``"t"`` is a trade, ``"success"``/``"subscription"`` are control messages,
    ``"error"`` is an error. Anything unrecognised is ignored rather than fatal:
    an unknown message type must never take the price feed down.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []

    ticks: list[TradeTick] = []
    for msg in payload:
        if not isinstance(msg, dict):
            continue
        kind = msg.get("T")
        if kind == "error":
            log.warning("market stream error message: %s", msg)
            continue
        if kind != "t":
            continue
        try:
            price = float(msg["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        ticks.append(
            TradeTick(
                symbol=str(msg.get("S", "")).upper(),
                price=price,
                size=float(msg.get("s", 0) or 0),
                at=_ts(msg.get("t")),
            )
        )
    return ticks


def parse_trade_update(raw: str | bytes) -> OrderEvent | None:
    """Parse one frame from the trade-updates stream.

    Two shapes are handled, because which one arrives depends on the endpoint
    generation [verify-at-runtime]:

    * legacy: ``{"stream": "trade_updates", "data": {"event": ..., "order": {...}}}``
    * flat:   ``{"event": ..., "order": {...}}``

    Control frames (auth/listen acknowledgements) return ``None``.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    if payload.get("stream") == "authorization":
        status = (payload.get("data") or {}).get("status")
        if status != "authorized":
            log.warning("trade-updates stream auth failed: %s", payload)
        return None
    if payload.get("stream") == "listening":
        return None

    data = payload.get("data") if payload.get("stream") == "trade_updates" else payload
    if not isinstance(data, dict):
        return None
    event = data.get("event")
    order = data.get("order")
    if not event or not isinstance(order, dict):
        return None

    def _f(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    price = order.get("filled_avg_price")
    return OrderEvent(
        event=str(event),
        client_order_id=str(order.get("client_order_id", "")),
        symbol=str(order.get("symbol", "")).upper(),
        filled_qty=_f(order.get("filled_qty")),
        filled_avg_price=_f(price) if price not in (None, "") else None,
        status=str(order.get("status", "")),
        raw=data,
    )


# --------------------------------------------------------------------------- #
# Live price cache shared between the stream thread and the bot.
# --------------------------------------------------------------------------- #

class LivePrice:
    """Thread-safe cache of the most recent trade.

    ``get(max_age)`` returns ``None`` when the cache is stale, which callers must
    treat as "fall back to REST" -- a stale price is worse than a slow one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._price: float | None = None
        self._at: float = 0.0

    def update(self, tick: TradeTick, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        with self._lock:
            self._price = tick.price
            self._at = monotonic()

    def get(self, max_age_s: float = 30.0, *, monotonic: Callable[[], float] = time.monotonic) -> float | None:
        with self._lock:
            if self._price is None or (monotonic() - self._at) > max_age_s:
                return None
            return self._price


# --------------------------------------------------------------------------- #
# Socket lifecycle
# --------------------------------------------------------------------------- #

class StreamWorker(threading.Thread):
    """One websocket connection, kept alive with capped backoff.

    ``connect`` is injected so tests can drive the worker with a fake socket.
    The real factory (see :func:`market_data_worker`) wraps ``websocket-client``.
    A worker failure is logged and retried forever; it never raises out of the
    thread, because the poll loop it accelerates does not depend on it.
    """

    def __init__(
        self,
        name: str,
        connect: Callable[[], Any],
        on_message: Callable[[str | bytes], None],
        *,
        stop: threading.Event | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(name=f"stream-{name}", daemon=True)
        self._connect = connect
        self._on_message = on_message
        self.stop_event = stop or threading.Event()
        self._sleeper = sleeper
        self.connected = threading.Event()
        self.n_messages = 0
        self.n_reconnects = 0

    def run(self) -> None:  # pragma: no cover - exercised via drive() in tests
        backoff = RECONNECT_BASE_S
        while not self.stop_event.is_set():
            try:
                sock = self._connect()
                self.connected.set()
                while not self.stop_event.is_set():
                    frame = sock.recv()
                    if frame is None or frame == "":
                        raise ConnectionError("stream closed by peer")
                    self._on_message(frame)
                    self.n_messages += 1
                    # Reset the backoff only once the connection has proven
                    # healthy by delivering a message. Resetting right after
                    # connect() would turn a server that accepts the handshake
                    # and immediately drops us (bad credentials, connection
                    # limit) into a tight 2-second hammer loop forever.
                    backoff = RECONNECT_BASE_S
            except Exception as exc:
                self.connected.clear()
                if self.stop_event.is_set():
                    break
                self.n_reconnects += 1
                log.warning("%s: %s; reconnecting in %.0fs", self.name, exc, backoff)
                self._sleeper(backoff)
                backoff = min(backoff * 2.0, RECONNECT_MAX_S)
        try:
            sock.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass

    def drive(self, frames: list[str], *, raise_after: Exception | None = None) -> None:
        """Test helper: feed frames through the message path synchronously."""
        for frame in frames:
            self._on_message(frame)
            self.n_messages += 1
        if raise_after is not None:
            raise raise_after

    def stop(self) -> None:
        self.stop_event.set()


class _PingingSocket:
    """Wrapper that turns idle-read timeouts into pings instead of reconnects.

    The trade-updates stream can be silent for hours (there are exactly two
    order bursts a day) and the market stream is silent overnight. If a read
    timeout were treated as a dead connection, an always-on process would
    reconnect and re-authenticate every timeout interval all night -- thousands
    of pointless handshakes. Instead, a timeout sends a ping and keeps reading;
    only a failed ping (or any other error) escalates to the reconnect path.
    """

    MAX_IDLE_PINGS = 120  # safety valve: an hour of pure silence at 30s reads

    def __init__(self, sock, timeout_exc: type[Exception]) -> None:
        self._sock = sock
        self._timeout_exc = timeout_exc

    def recv(self):
        idle = 0
        while True:
            try:
                return self._sock.recv()
            except self._timeout_exc:
                idle += 1
                if idle > self.MAX_IDLE_PINGS:
                    raise ConnectionError("stream idle far beyond any plausible quiet period") from None
                self._sock.ping()  # raises if the peer is truly gone -> reconnect path

    def send(self, data):
        return self._sock.send(data)

    def close(self):
        return self._sock.close()


def _ws_connect(url: str, *, auth_frames: list[dict], timeout: int = 20):
    """Open a websocket and perform the auth handshake. [verify-at-runtime]"""
    import websocket  # imported lazily: optional dependency

    sock = websocket.create_connection(url, timeout=timeout)
    sock.settimeout(30)  # short read timeout is fine: idle reads ping, not reconnect
    for frame in auth_frames:
        sock.send(json.dumps(frame))
    return _PingingSocket(sock, websocket.WebSocketTimeoutException)


def market_data_worker(
    key: str, secret: str, symbol: str, live_price: LivePrice, *, feed: str = "iex",
) -> StreamWorker:
    """Worker streaming live trades for ``symbol`` into ``live_price``.

    Deliberately does NOT wake the scheduler loop: a liquid stock prints many
    times a second, and waking on every tick would turn the poll loop into a
    busy loop. Price ticks refresh the cache; only *order events* wake.
    """
    url = MARKET_DATA_URL.format(feed=feed)

    def connect():
        return _ws_connect(
            url,
            auth_frames=[
                {"action": "auth", "key": key, "secret": secret},
                {"action": "subscribe", "trades": [symbol.upper()]},
            ],
        )

    def on_message(frame: str | bytes) -> None:
        for tick in parse_market_message(frame):
            if tick.symbol == symbol.upper():
                live_price.update(tick)

    return StreamWorker("market", connect, on_message)


def trade_updates_worker(
    key: str, secret: str, symbol: str, *,
    wake: threading.Event,
    base_url: str = "https://paper-api.alpaca.markets",
    on_event: Callable[[OrderEvent], None] | None = None,
) -> StreamWorker:
    """Worker that wakes the bot the moment one of its orders changes state."""
    url = base_url.replace("https://", "wss://").rstrip("/") + "/stream"

    def connect():
        return _ws_connect(
            url,
            auth_frames=[
                {"action": "auth", "key": key, "secret": secret},
                {"action": "listen", "data": {"streams": ["trade_updates"]}},
            ],
        )

    def on_message(frame: str | bytes) -> None:
        event = parse_trade_update(frame)
        if event is None:
            return
        if event.symbol and event.symbol != symbol.upper():
            return
        log.info("order event: %s %s (%s)", event.event, event.client_order_id, event.status)
        if on_event is not None:
            on_event(event)
        if event.is_actionable:
            wake.set()

    return StreamWorker("trade-updates", connect, on_message)
