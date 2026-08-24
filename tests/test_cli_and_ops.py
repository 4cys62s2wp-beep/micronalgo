"""CLI surface, preflight, reconciliation and the instance lock."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from micronalgo.calendar_nyse import NY, Calendar, ExchangeCalendarsSource
from micronalgo.cli import main
from micronalgo.config import Settings
from micronalgo.data.synthetic import random_walk
from micronalgo.live.broker import BrokerError, OrderSide, TimeInForce
from micronalgo.live.preflight import run_preflight
from micronalgo.live.reconcile import reconcile
from micronalgo.live.simbroker import SimBroker
from micronalgo.live.state import BotState, InstanceLock, Phase


@pytest.fixture
def sim_rig(settings):
    bars = random_walk(60, seed=1, start="2025-06-02")
    cal = Calendar([ExchangeCalendarsSource()], strict=True)
    broker = SimBroker(bars=bars, cash=100_000.0, calendar=cal,
                       start_at=dt.datetime(2025, 6, 10, 11, 0, tzinfo=NY))
    return settings, broker, cal, bars


# ------------------------------------------------------------------ preflight
def test_preflight_passes_against_a_healthy_broker(sim_rig):
    settings, broker, cal, _ = sim_rig
    report = run_preflight(settings, broker, cal, probe_orders=True,
                           now=dt.datetime(2025, 6, 10, 11, 0, tzinfo=NY))
    assert report.ok, report.render()
    names = {c.name for c in report.checks}
    assert {"account", "calendar_agreement", "order_type_cls", "order_type_opg"} <= names


def test_preflight_probe_leaves_no_live_orders(sim_rig):
    settings, broker, cal, _ = sim_rig
    run_preflight(settings, broker, cal, probe_orders=True,
                  now=dt.datetime(2025, 6, 10, 11, 0, tzinfo=NY))
    assert not broker.list_open_orders("MU")


def test_preflight_refuses_to_probe_near_the_close(sim_rig):
    settings, broker, cal, _ = sim_rig
    broker.set_now(dt.datetime(2025, 6, 10, 15, 40, tzinfo=NY))
    report = run_preflight(settings, broker, cal, probe_orders=True,
                           now=dt.datetime(2025, 6, 10, 15, 40, tzinfo=NY))
    assert any("too close to the closing auction" in c.detail for c in report.checks)


def test_preflight_reports_a_broken_broker(sim_rig):
    settings, broker, cal, _ = sim_rig

    class Dead:
        name = "dead"

        def get_account(self):
            raise BrokerError("401 unauthorized")

    report = run_preflight(settings, Dead(), cal)
    assert not report.ok
    assert "cannot read the account" in report.failures[0].detail


def test_preflight_flags_a_calendar_disagreement(sim_rig):
    settings, broker, cal, _ = sim_rig

    class Liar(SimBroker):
        def get_calendar(self, start, end):
            return {dt.date(2025, 6, 11): (dt.time(9, 30), dt.time(11, 11))}

    liar = Liar(bars=broker.bars, calendar=cal,
                start_at=dt.datetime(2025, 6, 10, 11, 0, tzinfo=NY))
    report = run_preflight(settings, liar, cal, now=dt.datetime(2025, 6, 10, 11, 0, tzinfo=NY))
    assert not report.ok
    assert any(c.name == "calendar_agreement" and not c.ok for c in report.checks)


# -------------------------------------------------------------- reconciliation
def test_reconcile_closes_a_trade_the_broker_no_longer_holds(sim_rig):
    settings, broker, _, _ = sim_rig
    state = BotState(symbol="MU")
    t = state.trade(dt.date(2025, 6, 9))
    t.phase = Phase.ENTRY_FILLED
    t.entry.filled_qty = 100
    result = reconcile(broker, state, symbol="MU")
    assert t.phase is Phase.CLOSED
    assert not result.halted
    assert any("broker flat" in c for c in result.changes)


def test_reconcile_halts_on_an_unattributable_position(sim_rig):
    settings, broker, _, _ = sim_rig
    broker.set_now(dt.datetime(2025, 6, 10, 12, 0, tzinfo=NY))
    broker.submit_order(symbol="MU", side=OrderSide.BUY, qty=50,
                        time_in_force=TimeInForce.DAY, client_order_id="someone-elses-order")
    state = BotState(symbol="MU")
    result = reconcile(broker, state, symbol="MU")
    assert result.halted and state.halted
    assert "did not open" in state.halt_reason


def test_reconcile_on_a_clean_slate_is_quiet(sim_rig):
    settings, broker, _, _ = sim_rig
    result = reconcile(broker, BotState(symbol="MU"), symbol="MU")
    assert result.clean


# ------------------------------------------------------------------ lock
def test_instance_lock_excludes_a_second_holder(tmp_path):
    path = tmp_path / ".lock"
    first = InstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="another micronalgo instance"):
            InstanceLock(path).acquire()
    finally:
        first.release()
    InstanceLock(path).acquire()  # released, so it is available again


# ------------------------------------------------------------------- CLI
def test_demo_runs_offline_and_writes_a_report(tmp_path, capsys):
    code = main(["demo", "--sessions", "400", "--resamples", "40", "--out", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "REALITY CHECK" in out and "RANDOM DATA" in out
    assert (tmp_path / "demo_synthetic.html").exists()
    payload = json.loads((tmp_path / "demo_synthetic.json").read_text())
    assert payload["verdict"] in ("PASS", "WARN", "FAIL")


def test_offline_fetch_without_a_cache_fails_loudly(tmp_path, capsys):
    code = main(["fetch", "--provider", "stooq", "--offline", "--cache-dir", str(tmp_path)])
    assert code == 1
    assert "csv:/path/to/MU.csv" in capsys.readouterr().err


def test_kill_and_resume_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MICRONALGO_KILL_SWITCH_FILE", str(tmp_path / "KILL"))
    monkeypatch.setenv("MICRONALGO_STATE_DIR", str(tmp_path))
    assert main(["kill", "--reason", "test"]) == 0
    assert (tmp_path / "KILL").exists()
    assert main(["resume"]) == 0
    assert not (tmp_path / "KILL").exists()


def test_status_emits_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MICRONALGO_STATE_DIR", str(tmp_path))
    assert main(["status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["symbol"] == "MU" and payload["open_positions"] == []


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for cmd in ("demo", "fetch", "validate", "study", "backtest", "preflight", "paper", "tick"):
        assert cmd in out


# ------------------------------------------------------------------ settings
def test_real_money_requires_an_explicit_acknowledgement():
    with pytest.raises(ValueError, match="not the paper endpoint"):
        Settings(broker="alpaca", alpaca_base_url="https://api.alpaca.markets")
    ok = Settings(broker="alpaca", alpaca_base_url="https://api.alpaca.markets",
                  live_trading_ack="I UNDERSTAND THIS IS REAL MONEY")
    assert not ok.is_paper


def test_cutoffs_must_be_inside_the_submit_window():
    with pytest.raises(ValueError, match="entry_cutoff_offset_min"):
        Settings(entry_submit_offset_min=5, entry_cutoff_offset_min=10)


def test_defaults_are_the_safe_ones():
    s = Settings()
    assert s.dry_run is True
    assert s.broker == "sim"
    assert s.is_paper
    assert s.on_missed_entry == "skip"
    assert s.capital_fraction < 1.0


def test_secrets_are_redacted():
    s = Settings(alpaca_key_id="AKVERYSECRET", alpaca_secret_key="shhh")
    dumped = s.redacted()
    assert "AKVERYSECRET" not in json.dumps(dumped)
    assert dumped["alpaca_key_id"].startswith("<set:")
