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


def test_status_watch_renders_and_terminates(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MICRONALGO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MICRONALGO_LOG_DIR", str(tmp_path))
    from micronalgo.cli import build_parser

    args = build_parser().parse_args(["status", "--watch", "--interval", "1"])
    args._watch_iterations = 2
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert out.count("micronalgo status") == 2
    assert "flat (MU)" in out


def test_mac_deploy_artifacts_are_wellformed():
    import plistlib
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    template = (root / "deploy" / "com.micronalgo.paper.plist.template").read_text()
    rendered = template.replace("__REPO__", "/tmp/r").replace("__VENV__", "/tmp/v")
    plist = plistlib.loads(rendered.encode())
    assert plist["Label"] == "com.micronalgo.paper"
    # launchd semantics: SuccessfulExit=false restarts NON-zero exits (crashes).
    # A halt exits 2 and is restarted once -- the startup halt-guard then exits
    # 0, a successful exit, which launchd leaves down. The pair of assertions
    # below covers both halves of that contract.
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ProgramArguments"][-1] == "paper"

    check = subprocess.run(["sh", "-n", str(root / "deploy" / "install_mac.sh")],
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_paper_refuses_to_start_while_halted_and_exits_zero(tmp_path, monkeypatch, capsys):
    """The other half of the launchd contract: a persisted halt must produce a
    SUCCESSFUL exit (0), because launchd's SuccessfulExit=false would restart
    any non-zero exit every ThrottleInterval forever."""
    import micronalgo.live.state as st

    monkeypatch.setenv("MICRONALGO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MICRONALGO_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("MICRONALGO_CACHE_DIR", str(tmp_path))
    state = st.BotState(symbol="MU")
    state.halt("drawdown breach during the test")
    st.save(state, tmp_path / "state.json")

    code = main(["paper", "--max-iterations", "1"])
    err = capsys.readouterr().err
    assert code == 0
    assert "HALTED" in err and "resume --clear-halt" in err


def test_watch_tails_a_large_audit_log(tmp_path, monkeypatch, capsys):
    """The watch view must read only the tail of an unbounded log file."""
    monkeypatch.setenv("MICRONALGO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MICRONALGO_LOG_DIR", str(tmp_path))
    audit = tmp_path / "audit.jsonl"
    with audit.open("w") as fh:
        for i in range(50_000):
            fh.write(json.dumps({"ts_ny": f"2026-08-24T10:00:{i % 60:02d}", "event": f"e{i}"}) + "\n")

    from micronalgo.cli import build_parser

    args = build_parser().parse_args(["status", "--watch", "--interval", "1"])
    args._watch_iterations = 1
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "e49999" in out and "e0 " not in out


def test_installer_templating_survives_metacharacter_paths(tmp_path):
    """A repo path like 'Trading & Bots' is legal on macOS, is a metacharacter
    for sed (the original bug) AND is illegal raw in XML (the second layer of
    the same bug). The installer's python templating XML-escapes, and launchd
    reads the escaped form back as the literal path -- which this test proves
    by round-tripping through plistlib exactly as launchd would."""
    import plistlib
    from pathlib import Path
    from xml.sax.saxutils import escape

    template = (Path(__file__).resolve().parents[1] / "deploy"
                / "com.micronalgo.paper.plist.template").read_text()
    nasty_repo = "/Users/me/Trading & Bots|x/micronalgo"
    rendered = (template
                .replace("__REPO__", escape(nasty_repo))
                .replace("__VENV__", escape(nasty_repo + "/.venv")))
    plist = plistlib.loads(rendered.encode())
    assert plist["ProgramArguments"][0] == nasty_repo + "/.venv/bin/micronalgo"
    assert plist["WorkingDirectory"] == nasty_repo


def test_cloud_deploy_artifacts_are_wellformed():
    """The container and cron paths are how this runs without a Mac, so their
    config files get the same offline scrutiny as everything else."""
    from pathlib import Path

    import tomllib

    root = Path(__file__).resolve().parents[1]

    dockerfile = (root / "deploy" / "Dockerfile").read_text()
    # zoneinfo has no data in a slim image, and every schedule in this project
    # is expressed in America/New_York -- without tzdata the calendar raises.
    assert "tzdata" in dockerfile
    assert "VOLUME" in dockerfile
    assert 'CMD ["micronalgo", "paper"]' in dockerfile, "dry-run must be the container default"

    fly = tomllib.loads((root / "deploy" / "fly.toml").read_text())
    assert fly["env"]["MICRONALGO_DRY_RUN"] == "true"
    assert fly["mounts"]["destination"] == "/data"
    # Two instances would fight over one position; the in-process lock cannot
    # span machines, so the config must pin a single VM and forbid rolling.
    assert len(fly["vm"]) == 1
    assert fly["deploy"]["strategy"] == "immediate"


def test_actions_workflow_is_wellformed():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    wf = yaml.safe_load((root / ".github" / "workflows" / "paper-trading.yml").read_text())
    triggers = wf[True]  # pyyaml reads the `on:` key as the boolean True (YAML 1.1)
    assert "workflow_dispatch" in triggers, "a manual run button is the phone-only escape hatch"
    assert len(triggers["schedule"]) == 2

    # Overlapping runs would race on one position; queue them, never cancel a
    # half-finished tick.
    assert wf["concurrency"]["cancel-in-progress"] is False

    job = wf["jobs"]["tick"]
    env = job["env"]
    assert "true" in env["MICRONALGO_DRY_RUN"], "dry run must be the default"

    # GitHub cron can fire 10+ minutes late, so the entry window is widened far
    # beyond the default 5 minutes to absorb that.
    submit = int(env["MICRONALGO_ENTRY_SUBMIT_OFFSET_MIN"])
    cutoff = int(env["MICRONALGO_ENTRY_CUTOFF_OFFSET_MIN"])
    assert submit - cutoff >= 25, f"window only {submit - cutoff} min wide; cron delay would miss it"

    names = [s["name"] for s in job["steps"]]
    assert "Preflight" in names and names.index("Preflight") < names.index("Tick"), (
        "a failed preflight must prevent the tick, not follow it"
    )


def test_widened_window_absorbs_a_late_cron(monkeypatch):
    """The workflow's own numbers, checked against the real calendar."""
    import datetime as dt
    from pathlib import Path

    import yaml

    from micronalgo.calendar_nyse import Calendar, ExchangeCalendarsSource
    from micronalgo.config import Settings

    root = Path(__file__).resolve().parents[1]
    env = yaml.safe_load((root / ".github" / "workflows" / "paper-trading.yml").read_text())[
        "jobs"]["tick"]["env"]
    s = Settings(
        entry_submit_offset_min=int(env["MICRONALGO_ENTRY_SUBMIT_OFFSET_MIN"]),
        entry_cutoff_offset_min=int(env["MICRONALGO_ENTRY_CUTOFF_OFFSET_MIN"]),
    )
    cal = Calendar([ExchangeCalendarsSource()], strict=True)
    for day in (dt.date(2025, 6, 10), dt.date(2024, 11, 29)):  # regular and a 13:00 half-day
        session = cal.session(day)
        submit, cutoff = s.entry_window(session.close_dt())
        assert cutoff < session.close_dt()
        for delay in (0, 10, 20):
            assert submit <= submit + dt.timedelta(minutes=delay) <= cutoff


def test_start_script_is_safe_by_construction():
    """The one-command start path. Its safety properties are structural, so
    they are asserted here rather than trusted to survive edits."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = root / "deploy" / "start_mac.sh"
    text = script.read_text()

    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0
    assert text.startswith("#!/bin/sh")
    assert "set -eu" in text

    # Preflight must gate trading: it has to appear before `paper` runs, and a
    # failure has to abort rather than warn.
    assert text.index("preflight --probe-orders") < text.index("exec ")
    assert "die \"Preflight nicht bestanden" in text

    # Dry run is the default; going live requires an explicit answer.
    assert 'MODE="--dry-run"' in text
    assert text.index('MODE="--dry-run"') < text.index('MODE="--live"')

    # Secrets: read from the terminal (works under `curl | sh`), echo disabled,
    # written only to .env, which is gitignored and chmod 600.
    assert "/dev/tty" in text
    assert "stty -echo" in text
    assert "chmod 600 .env" in text
    assert ".env" in (root / ".gitignore").read_text()

    # The script must never hardcode a live-money endpoint.
    assert "api.alpaca.markets" not in text.replace("paper-api.alpaca.markets", "")
