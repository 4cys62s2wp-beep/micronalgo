"""Broker adapter, sim broker, state persistence and risk guards."""

from __future__ import annotations

import datetime as dt

import pytest

from micronalgo.calendar_nyse import NY, Calendar, ExchangeCalendarsSource
from micronalgo.data.synthetic import random_walk
from micronalgo.live.alpaca import parse_account, parse_calendar, parse_order, parse_position
from micronalgo.live.broker import (
    Account,
    DuplicateOrderError,
    OrderSide,
    OrderStatus,
    PermanentBrokerError,
    TimeInForce,
    make_client_order_id,
    parse_client_order_id,
)
from micronalgo.live.risk import (
    ErrorBudget,
    RiskContext,
    engage_kill_switch,
    evaluate,
    kill_switch_active,
    release_kill_switch,
)
from micronalgo.live.simbroker import SimBroker
from micronalgo.live.state import BotState, Phase, StateVersionError, load, save
from micronalgo.live.strategy import target_shares


# ------------------------------------------------------------- order identity
def test_client_order_id_is_deterministic_and_reversible():
    a = make_client_order_id("mna", "MU", dt.date(2026, 8, 25), "buy")
    b = make_client_order_id("mna", "MU", dt.date(2026, 8, 25), "buy")
    assert a == b == "mna-MU-2026-08-25-BUY-0"
    parsed = parse_client_order_id(a)
    assert parsed["trade_date"] == "2026-08-25" and parsed["leg"] == "BUY"


def test_both_legs_of_one_trade_share_the_entry_date():
    buy = make_client_order_id("mna", "MU", dt.date(2026, 8, 25), "buy")
    sell = make_client_order_id("mna", "MU", dt.date(2026, 8, 25), "sell")
    assert parse_client_order_id(buy)["trade_date"] == parse_client_order_id(sell)["trade_date"]


def test_foreign_ids_are_not_claimed():
    assert parse_client_order_id("some-other-system-id") is None


# ------------------------------------------------------------ alpaca parsers
def test_order_parser_and_terminal_states():
    o = parse_order({"id": "x", "client_order_id": "c", "symbol": "MU", "side": "buy",
                     "qty": "250", "type": "market", "time_in_force": "cls", "status": "filled",
                     "filled_qty": "250", "filled_avg_price": "98.735"})
    assert o.is_filled and o.status.is_terminal and o.time_in_force is TimeInForce.CLS


def test_unknown_status_does_not_crash():
    o = parse_order({"id": "x", "client_order_id": "c", "symbol": "MU", "side": "buy",
                     "qty": "1", "status": "some_new_status"})
    assert o.status is OrderStatus.UNKNOWN and o.status.is_open


def test_account_and_position_parsers():
    a = parse_account({"equity": "101234.56", "cash": "1000", "buying_power": "2000",
                       "trading_blocked": True})
    assert a.blocked
    p = parse_position({"symbol": "MU", "qty": "100", "avg_entry_price": "98.5"})
    assert not p.is_flat and p.qty == 100


def test_calendar_parser_keeps_half_days():
    cal = parse_calendar([{"date": "2026-11-27", "open": "09:30", "close": "13:00"},
                          {"date": "bad", "open": "x", "close": "y"}])
    assert cal[dt.date(2026, 11, 27)][1] == dt.time(13, 0)
    assert len(cal) == 1  # the malformed row is dropped, not fatal


# ---------------------------------------------------------------- sim broker
@pytest.fixture
def sim():
    bars = random_walk(30, seed=2, start="2025-06-02")
    cal = Calendar([ExchangeCalendarsSource()], strict=True)
    return SimBroker(bars=bars, cash=100_000.0, calendar=cal), bars, cal


def test_auction_orders_fill_at_the_recorded_prints(sim):
    sb, bars, cal = sim
    d0, d1 = bars.index[3].date(), bars.index[4].date()

    sb.set_now(dt.datetime.combine(d0, dt.time(15, 45), tzinfo=NY))
    buy = sb.submit_order(symbol="MU", side=OrderSide.BUY, qty=100,
                          time_in_force=TimeInForce.CLS, client_order_id="b1")
    assert buy.status.is_open
    sb.set_now(dt.datetime.combine(d0, dt.time(16, 5), tzinfo=NY))
    assert sb.get_order_by_client_id("b1").filled_avg_price == pytest.approx(
        float(bars.loc[str(d0), "raw_close"]))

    sb.set_now(dt.datetime.combine(d1, dt.time(8, 30), tzinfo=NY))
    sb.submit_order(symbol="MU", side=OrderSide.SELL, qty=100,
                    time_in_force=TimeInForce.OPG, client_order_id="s1")
    sb.set_now(dt.datetime.combine(d1, dt.time(9, 35), tzinfo=NY))
    assert sb.get_order_by_client_id("s1").filled_avg_price == pytest.approx(
        float(bars.loc[str(d1), "raw_open"]))
    assert sb.get_position("MU") is None


def test_duplicate_client_id_is_rejected(sim):
    sb, bars, _ = sim
    sb.set_now(dt.datetime.combine(bars.index[3].date(), dt.time(15, 45), tzinfo=NY))
    sb.submit_order(symbol="MU", side=OrderSide.BUY, qty=1,
                    time_in_force=TimeInForce.CLS, client_order_id="dup")
    with pytest.raises(DuplicateOrderError):
        sb.submit_order(symbol="MU", side=OrderSide.BUY, qty=1,
                        time_in_force=TimeInForce.CLS, client_order_id="dup")


def test_auction_cutoffs_are_enforced(sim):
    sb, bars, _ = sim
    sb.set_now(dt.datetime.combine(bars.index[3].date(), dt.time(15, 55), tzinfo=NY))
    with pytest.raises(PermanentBrokerError, match="after the cutoff"):
        sb.submit_order(symbol="MU", side=OrderSide.BUY, qty=1,
                        time_in_force=TimeInForce.CLS, client_order_id="late")


def test_clock_cannot_move_backwards(sim):
    sb, bars, _ = sim
    sb.set_now(dt.datetime.combine(bars.index[5].date(), dt.time(12, 0), tzinfo=NY))
    with pytest.raises(ValueError, match="backwards"):
        sb.set_now(dt.datetime.combine(bars.index[4].date(), dt.time(12, 0), tzinfo=NY))


def test_intraday_price_interpolates_towards_the_close(sim):
    """Sizing at 15:45 must use a price near the close, not the stale open."""
    sb, bars, _ = sim
    day = bars.index[6].date()
    o, c = float(bars.loc[str(day), "raw_open"]), float(bars.loc[str(day), "raw_close"])
    sb.set_now(dt.datetime.combine(day, dt.time(15, 45), tzinfo=NY))
    px = sb.get_last_price("MU")
    assert min(o, c) - 1e-9 <= px <= max(o, c) + 1e-9
    assert abs(px - c) < abs(px - o) or o == pytest.approx(c)


# --------------------------------------------------------------------- state
def test_state_roundtrip_and_backup_recovery(tmp_path):
    path = tmp_path / "state.json"
    s = BotState(symbol="MU")
    t = s.trade(dt.date(2026, 8, 25))
    t.phase = Phase.ENTRY_FILLED
    t.entry.filled_qty = 250
    save(s, path)
    save(s, path)  # creates the .bak

    assert load(path).trade("2026-08-25").phase is Phase.ENTRY_FILLED

    path.write_text("{ truncated")
    assert load(path).trade("2026-08-25").phase is Phase.ENTRY_FILLED  # from .bak


def test_both_copies_damaged_starts_fresh_and_warns(tmp_path):
    path = tmp_path / "state.json"
    save(BotState(symbol="MU"), path)
    save(BotState(symbol="MU"), path)
    path.write_text("{bad")
    path.with_suffix(".json.bak").write_text("{also bad")
    with pytest.warns(RuntimeWarning, match="reconciliation"):
        fresh = load(path)
    assert fresh.trades == {}
    assert path.with_suffix(".json.corrupt").exists()


def test_newer_schema_refuses_to_load(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"version": 99, "symbol": "MU", "trades": {}}')
    with pytest.raises(StateVersionError):
        load(path)


def test_prune_keeps_unresolved_trades(tmp_path):
    s = BotState()
    for i in range(1, 20):
        t = s.trade(dt.date(2025, 1, 1) + dt.timedelta(days=i))
        t.phase = Phase.CLOSED
    held = s.trade(dt.date(2024, 1, 1))
    held.phase = Phase.ENTRY_FILLED
    s.prune(keep=5)
    assert "2024-01-01" in s.trades
    assert len([t for t in s.trades.values() if t.phase is Phase.CLOSED]) == 5


# ---------------------------------------------------------------------- risk
def _acct(**kw):
    base = {"equity": 100_000.0, "cash": 100_000.0,
            "buying_power": 100_000.0, "last_equity": 100_000.0}
    base.update(kw)
    return Account(**base)


def test_clean_context_allows(settings):
    v = evaluate(settings, BotState(), RiskContext(account=_acct(), reference_price=98.5,
                                                  last_known_close=97.0, data_age_days=1,
                                                  asset_tradable=True, intended_notional=95_000,
                                                  intended_shares=964))
    assert v.allow, v.reason


@pytest.mark.parametrize("ctx_kw,state_kw,match", [
    ({"asset_tradable": False}, {}, "not tradable"),
    ({"reference_price": 49.0, "last_known_close": 98.0}, {}, "deviates"),
    pytest.param({"data_age_days": 30}, {}, "stale", id="stale-data"),
    ({"intended_notional": 10_000_000.0}, {}, "buying power"),
    ({}, {"consecutive_losses": 99}, "consecutive"),
    ({"account": _acct(trading_blocked=True)}, {}, "blocked"),
    ({"account": _acct(equity=60_000.0), "intended_notional": 1000.0}, {"equity_peak": 150_000.0}, "drawdown"),
    ({"account": _acct(equity=90_000.0, last_equity=100_000.0), "intended_notional": 1000.0}, {}, "daily loss"),
])
def test_each_guard_vetoes(settings, ctx_kw, state_kw, match):
    # The shared fixture disables the staleness guard so the sim-broker tests can
    # replay historical bars; this test needs the production default back.
    settings = settings.model_copy(update={"max_data_age_days": 5})
    state = BotState(**state_kw)
    ctx = {"account": _acct(), "reference_price": 98.5, "last_known_close": 97.0,
           "data_age_days": 1, "asset_tradable": True,
           "intended_notional": 95_000, "intended_shares": 964}
    ctx.update(ctx_kw)
    v = evaluate(settings, state, RiskContext(**ctx))
    assert not v.allow
    assert match in v.reason, v.reason


def test_kill_switch_round_trip(settings):
    assert not kill_switch_active(settings.kill_switch_file)
    engage_kill_switch(settings.kill_switch_file, "test")
    assert not evaluate(settings, BotState(), RiskContext(account=_acct())).allow
    assert release_kill_switch(settings.kill_switch_file)
    assert not kill_switch_active(settings.kill_switch_file)


def test_error_budget_trips_and_expires():
    now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    eb = ErrorBudget(3, 30)
    for i in range(3):
        eb.record(now + dt.timedelta(minutes=i))
    assert eb.tripped(now + dt.timedelta(minutes=5))
    assert not eb.tripped(now + dt.timedelta(minutes=40))


# -------------------------------------------------------------------- sizing
def test_sizing_respects_every_cap(settings):
    s = settings.model_copy(update={"max_notional": 10_000.0})
    assert target_shares(1_000_000.0, 100.0, s).notional <= 10_000.0

    s2 = settings.model_copy(update={"max_shares": 5})
    assert target_shares(1_000_000.0, 100.0, s2).shares == 5

    s3 = settings.model_copy(update={"max_participation_of_adv": 0.001})
    assert target_shares(1_000_000.0, 100.0, s3, adv=1000.0).shares == 1


def test_sizing_leaves_buying_power_headroom(settings):
    d = target_shares(100_000.0, 100.0, settings)
    assert d.notional < 100_000.0


def test_sizing_refuses_nonsense(settings):
    assert not target_shares(0.0, 100.0, settings).tradable
    assert not target_shares(100_000.0, 0.0, settings).tradable


# ------------------------------------------------- die Eroeffnungsauktion
# Der Preflight meldete hier einmal FAIL, weil er mitten in der Sitzung lief:
# die Auktion nimmt 'opg' nur ausserhalb der Handelszeit an. Das ist eine
# Aussage ueber die Uhrzeit, nicht ueber das Konto -- und wurde prompt als
# "falsche Schluessel" fehlgedeutet.

def test_opg_window_matches_the_auction_rule():
    import datetime as dt

    from micronalgo.calendar_nyse import NY
    from micronalgo.live.preflight import _opg_submittable

    def at(h, m):
        return _opg_submittable(dt.datetime(2026, 8, 26, h, m, tzinfo=NY))

    assert not at(15, 31), "mitten in der Sitzung nimmt die Auktion nichts an"
    assert not at(9, 30), "zur Eroeffnung ist es zu spaet"
    assert not at(9, 28), "09:28 ist der Annahmeschluss, nicht mehr davor"
    assert at(9, 27), "eine Minute vor Schluss geht noch"
    assert at(8, 30), "das Einreichfenster des Bots liegt hier"
    assert not at(18, 59), "vor 19:00 ist das Fenster noch zu"
    assert at(19, 0) and at(23, 30) and at(0, 15), "abends und nachts offen"


def test_the_bots_exit_window_lies_inside_the_auction_window(settings):
    """Die Vorgabe muss einreichen, solange die Auktion noch annimmt."""
    import datetime as dt

    from micronalgo.calendar_nyse import NY
    from micronalgo.live.preflight import _opg_submittable

    session_open = dt.datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    submit_at, cutoff = settings.exit_window(session_open)
    assert _opg_submittable(submit_at), "der Bot reicht zu frueh fuer die Auktion ein"
    assert _opg_submittable(cutoff), "der harte Cutoff liegt hinter dem Annahmeschluss"


def test_an_exit_window_past_the_auction_cutoff_is_refused():
    """Zu eng gesetzt wuerde die Ausstiegsorder abgelehnt -- mit offener Position."""
    import pytest

    from micronalgo.config import Settings

    for zu_eng in (2, 1, 0):
        with pytest.raises(ValueError, match="opening auction"):
            Settings(exit_cutoff_offset_min=zu_eng)


# --------------------------------------------------- der Trockenlauf muss reden
# Ein Trockenlauf, den man nicht sieht, ist kein Trockenlauf. Die Aktion
# 'dry_run' stand nicht in der Notifier-Menge und fiel damit stumm zu Boden --
# der Bot entschied korrekt, schrieb ins Audit-Log, und auf der Konsole blieb
# es still, waehrend jemand danebensass und auf ein Lebenszeichen wartete.

def test_every_action_reaches_the_console(caplog):
    import logging
    import threading

    from micronalgo.live.runner import Action
    from micronalgo.live.scheduler import NOTIFY_KINDS, run

    class Bot:
        """Gibt in der ersten Runde je eine stille und eine laute Aktion aus."""

        def __init__(self, settings):
            self.settings = settings
            self.calendar = type("C", (), {"authority": "test", "session": lambda *_: None})()
            self.broker = type("B", (), {"name": "stub"})()
            self.state = type("S", (), {"halted": False, "halt_reason": ""})()
            self.errors = type("E", (), {"record": lambda *_: None})()

        def tick(self, _now):
            return [
                Action("dry_run", "would BUY 37 @ ~940.12 on the close", "2026-08-26"),
                Action("skipped", "kein Handelstag", "2026-08-26"),
            ]

    from micronalgo.config import load_settings

    settings = load_settings(dry_run=True)
    stop = threading.Event()
    with caplog.at_level(logging.INFO):
        run(Bot(settings), settings=settings, stop=stop, max_iterations=1,
            sleeper=lambda _s: stop.set())

    text = caplog.text
    assert "would BUY 37" in text, "die Trockenlauf-Entscheidung fehlt auf der Konsole"
    assert "kein Handelstag" in text, "die laute Aktion fehlt ebenfalls"
    assert "dry_run" not in NOTIFY_KINDS, (
        "dry_run gehoert nicht in den Push-Weg -- es soll sichtbar sein, nicht alarmieren"
    )
    assert text.count("would BUY 37") == 1, "genau eine Zeile pro Aktion, keine Doppelung"
