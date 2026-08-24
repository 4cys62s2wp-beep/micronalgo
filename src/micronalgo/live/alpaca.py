"""Alpaca REST adapter.

Written against plain ``requests`` rather than a vendor SDK on purpose: the
build environment cannot reach the API, so every behaviour that *can* be pinned
down offline is a pure function over a JSON payload, tested in
``tests/test_alpaca.py``. What genuinely cannot be verified without the network
is marked ``[verify-at-runtime]`` and is checked by ``micronalgo preflight``
against the live paper account before a single order is ever sent.

Retry policy
------------
Retries happen only for errors that leave the order state *unknown* (timeout,
connection reset, 429, 5xx). A 4xx is a statement about the request and is
never retried. Critically, a retry reuses the **same** ``client_order_id``:
after a timeout the order may already be live, and a fresh id is exactly how
one intended position becomes two.
"""

from __future__ import annotations

import datetime as dt
import random
import time
from typing import Any
from zoneinfo import ZoneInfo

import requests

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
    TransientBrokerError,
)

NY = ZoneInfo("America/New_York")
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = 20


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_order(payload: dict[str, Any]) -> Order:
    """Pure parser for an Alpaca order object. Unit-tested offline."""
    return Order(
        client_order_id=str(payload.get("client_order_id", "")),
        symbol=str(payload.get("symbol", "")),
        side=OrderSide(str(payload.get("side", "buy")).lower()),
        qty=_f(payload.get("qty") or payload.get("notional")),
        order_type=OrderType(str(payload.get("type", payload.get("order_type", "market"))).lower())
        if str(payload.get("type", "market")).lower() in {"market", "limit"}
        else OrderType.MARKET,
        time_in_force=TimeInForce(str(payload.get("time_in_force", "day")).lower())
        if str(payload.get("time_in_force", "day")).lower() in {t.value for t in TimeInForce}
        else TimeInForce.DAY,
        limit_price=_f(payload["limit_price"]) if payload.get("limit_price") else None,
        broker_order_id=str(payload.get("id", "")),
        status=OrderStatus.parse(payload.get("status")),
        filled_qty=_f(payload.get("filled_qty")),
        filled_avg_price=_f(payload["filled_avg_price"]) if payload.get("filled_avg_price") else None,
        submitted_at=_parse_ts(payload.get("submitted_at") or payload.get("created_at")),
        filled_at=_parse_ts(payload.get("filled_at")),
        raw=payload,
    )


def parse_account(payload: dict[str, Any]) -> Account:
    return Account(
        equity=_f(payload.get("equity")),
        cash=_f(payload.get("cash")),
        buying_power=_f(payload.get("buying_power")),
        last_equity=_f(payload.get("last_equity")),
        currency=str(payload.get("currency", "USD")),
        trading_blocked=bool(payload.get("trading_blocked", False)),
        account_blocked=bool(payload.get("account_blocked", False)),
        pattern_day_trader=bool(payload.get("pattern_day_trader", False)),
        daytrade_count=int(_f(payload.get("daytrade_count"))),
        multiplier=_f(payload.get("multiplier"), 1.0),
        raw=payload,
    )


def parse_position(payload: dict[str, Any]) -> Position:
    return Position(
        symbol=str(payload.get("symbol", "")),
        qty=_f(payload.get("qty")),
        avg_entry_price=_f(payload.get("avg_entry_price")),
        market_value=_f(payload.get("market_value")),
        unrealized_pl=_f(payload.get("unrealized_pl")),
    )


def parse_calendar(rows: list[dict[str, Any]]) -> dict[dt.date, tuple[dt.time, dt.time]]:
    """Alpaca ``/v2/calendar`` -> ``{date: (open, close)}`` in exchange local time.

    This is the highest-authority calendar available: it is the schedule the
    broker itself enforces, including ad-hoc closures and 13:00 half-days.
    """
    out: dict[dt.date, tuple[dt.time, dt.time]] = {}
    for row in rows:
        try:
            day = dt.date.fromisoformat(str(row["date"]))
            o = dt.time.fromisoformat(str(row["open"]))
            c = dt.time.fromisoformat(str(row["close"]))
        except (KeyError, ValueError):
            continue
        out[day] = (o, c)
    return out


class AlpacaBroker:
    """REST client for Alpaca's trading API."""

    name = "alpaca"

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        base_url: str = "https://paper-api.alpaca.markets",
        data_url: str = "https://data.alpaca.markets",
        feed: str = "iex",
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 4,
        sleep=time.sleep,
        session: requests.Session | None = None,
    ) -> None:
        if not key_id or not secret_key:
            raise PermanentBrokerError(
                "Alpaca credentials missing. Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY."
            )
        self.base_url = base_url.rstrip("/")
        self.data_url = data_url.rstrip("/")
        self.feed = feed
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret_key,
                "Content-Type": "application/json",
                "User-Agent": "micronalgo/1.0",
            }
        )

    # ------------------------------------------------------------------ #

    def _request(self, method: str, url: str, *, params: dict | None = None, json_body: dict | None = None,
                 allow_404: bool = False) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.request(
                    method, url, params=params, json=json_body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_exc = TransientBrokerError(f"{method} {url}: {exc}")
            else:
                if resp.status_code == 404 and allow_404:
                    return None
                if 200 <= resp.status_code < 300:
                    if not resp.content:
                        return None
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise PermanentBrokerError(f"non-JSON response from {url}: {resp.text[:200]}") from exc

                body = resp.text[:400]
                lowered = body.lower()
                if resp.status_code in (409, 422) and "client_order_id" in lowered and (
                    "unique" in lowered or "exist" in lowered or "duplicate" in lowered
                ):
                    raise DuplicateOrderError(body)
                if resp.status_code in RETRY_STATUS:
                    last_exc = TransientBrokerError(f"HTTP {resp.status_code} from {url}: {body}")
                else:
                    raise PermanentBrokerError(f"HTTP {resp.status_code} from {url}: {body}")

            if attempt < self.max_retries:
                backoff = min(2.0**attempt, 16.0) * (1.0 + 0.25 * random.random())
                self._sleep(backoff)

        raise last_exc or TransientBrokerError(f"{method} {url} failed")

    # ------------------------------------------------------------------ #

    def get_account(self) -> Account:
        return parse_account(self._request("GET", f"{self.base_url}/v2/account"))

    def get_clock(self) -> Clock:
        p = self._request("GET", f"{self.base_url}/v2/clock")
        return Clock(
            timestamp=_parse_ts(p.get("timestamp")) or dt.datetime.now(dt.timezone.utc),
            is_open=bool(p.get("is_open", False)),
            next_open=_parse_ts(p.get("next_open")),
            next_close=_parse_ts(p.get("next_close")),
        )

    def get_calendar(self, start: dt.date, end: dt.date) -> dict[dt.date, tuple[dt.time, dt.time]]:
        rows = self._request(
            "GET", f"{self.base_url}/v2/calendar",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        return parse_calendar(rows or [])

    def get_position(self, symbol: str) -> Position | None:
        p = self._request("GET", f"{self.base_url}/v2/positions/{symbol.upper()}", allow_404=True)
        return parse_position(p) if p else None

    def list_open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {"status": "open", "limit": 200, "nested": "false"}
        if symbol:
            params["symbols"] = symbol.upper()
        rows = self._request("GET", f"{self.base_url}/v2/orders", params=params) or []
        return [parse_order(r) for r in rows]

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        p = self._request(
            "GET", f"{self.base_url}/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id}, allow_404=True,
        )
        return parse_order(p) if p else None

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
        body: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.value,
            "type": order_type.value,
            "time_in_force": time_in_force.value,
            "client_order_id": client_order_id,
            "qty": str(qty) if qty == int(qty) else f"{qty:.9f}".rstrip("0"),
        }
        if limit_price is not None:
            body["limit_price"] = f"{limit_price:.2f}"
        if extended_hours:
            body["extended_hours"] = True
        return parse_order(self._request("POST", f"{self.base_url}/v2/orders", json_body=body))

    def cancel_order(self, broker_order_id: str) -> None:
        self._request("DELETE", f"{self.base_url}/v2/orders/{broker_order_id}", allow_404=True)

    def get_last_price(self, symbol: str) -> float:
        """Latest trade price. Falls back to the latest quote midpoint.

        Used only for sanity checks and share sizing, never as a fill price.
        """
        try:
            p = self._request(
                "GET", f"{self.data_url}/v2/stocks/{symbol.upper()}/trades/latest",
                params={"feed": self.feed},
            )
            price = _f((p or {}).get("trade", {}).get("p"))
            if price > 0:
                return price
        except PermanentBrokerError:
            pass
        q = self._request(
            "GET", f"{self.data_url}/v2/stocks/{symbol.upper()}/quotes/latest",
            params={"feed": self.feed}, allow_404=True,
        )
        quote = (q or {}).get("quote", {})
        bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        raise TransientBrokerError(f"no usable last price for {symbol}")

    def is_tradable(self, symbol: str) -> bool:
        a = self._request("GET", f"{self.base_url}/v2/assets/{symbol.upper()}", allow_404=True)
        if not a:
            return False
        return bool(a.get("tradable", False)) and str(a.get("status", "")).lower() == "active"

    def supports_fractional(self, symbol: str) -> bool:
        a = self._request("GET", f"{self.base_url}/v2/assets/{symbol.upper()}", allow_404=True)
        return bool((a or {}).get("fractionable", False))
