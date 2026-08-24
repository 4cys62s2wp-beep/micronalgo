"""Command-line interface.

    micronalgo demo                 offline end-to-end demo, no network needed
    micronalgo fetch                download and cache the price history
    micronalgo validate             check a cached series for the defects that matter
    micronalgo study                the full research report (txt + html + json)
    micronalgo backtest             a single configuration, quickly
    micronalgo preflight            verify every runtime assumption against the broker
    micronalgo paper                run the bot
    micronalgo tick                 one idempotent tick (for cron or systemd timers)
    micronalgo status               what the bot currently believes
    micronalgo kill / resume        the kill switch
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .config import Settings, load_settings


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--symbol", default=None, help="ticker (default: MU or MICRONALGO_SYMBOL)")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--log-level", default=None)


def _settings(args: argparse.Namespace, **overrides) -> Settings:
    kw = dict(overrides)
    for name in ("symbol", "cache_dir", "log_level"):
        val = getattr(args, name, None)
        if val:
            kw[name] = val
    return load_settings(**kw)


def _load_bars(args: argparse.Namespace, settings: Settings, *, check_fresh: bool = False):
    from .data.loader import load

    providers = tuple(args.provider) if getattr(args, "provider", None) else settings.data_providers
    return load(
        settings.symbol,
        providers=providers,
        start=dt.date.fromisoformat(args.start) if getattr(args, "start", None) else None,
        end=dt.date.fromisoformat(args.end) if getattr(args, "end", None) else None,
        cache_dir=settings.cache_dir,
        refresh=getattr(args, "refresh", False),
        offline=getattr(args, "offline", False),
        check_fresh=check_fresh,
    )


def _calendar(broker=None, *, strict: bool = True):
    """Build the calendar, preferring the broker's own schedule."""
    import datetime as _dt

    from .calendar_nyse import BrokerCalendarSource, Calendar, default_sources

    sources = []
    if broker is not None:
        try:
            today = _dt.date.today()
            rows = broker.get_calendar(today - _dt.timedelta(days=30), today + _dt.timedelta(days=120))
            if rows:
                sources.append(BrokerCalendarSource(rows))
        except Exception:
            pass
    sources.extend(default_sources())
    return Calendar(sources, strict=strict)


def _broker(settings: Settings, bars=None):
    from .live.alpaca import AlpacaBroker
    from .live.simbroker import SimBroker

    if settings.broker == "alpaca":
        return AlpacaBroker(
            settings.alpaca_key_id, settings.alpaca_secret_key,
            base_url=settings.alpaca_base_url, feed=settings.alpaca_data_feed,
        )
    if bars is None:
        raise SystemExit("the 'sim' broker needs a price series; run 'micronalgo fetch' first")
    return SimBroker(bars=bars, cash=settings.initial_capital, calendar=_calendar(strict=False))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_demo(args: argparse.Namespace) -> int:
    """Everything, offline, on a synthetic series. Proves the install works."""
    from .data.synthetic import random_walk
    from .data.validate import validate
    from .research.report import write_report
    from .research.study import run_study

    print("Building a synthetic MU-like series (no network required)...")
    bars = random_walk(
        args.sessions, seed=args.seed, start="1994-01-03",
        overnight_mu=0.00065, overnight_sigma=0.021,
        intraday_mu=-0.00035, intraday_sigma=0.024,
        fat_tail_df=3.5, initial_close=8.0,
    )
    result = run_study(
        bars, symbol="SYNTHETIC",
        provenance="SYNTHETIC DATA - this demonstrates the machinery, it says nothing about MU",
        validation=validate(bars), bootstrap_resamples=args.resamples,
    )
    from .research.report import render_console
    print(render_console(result))
    paths = write_report(result, args.out or "reports", stem="demo_synthetic")
    print(f"\nwrote {paths['html']}")
    print("\nThese numbers come from RANDOM DATA. Run 'micronalgo study' with a real")
    print("provider to get the answer for Micron.")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    settings = _settings(args)
    loaded = _load_bars(args, settings)
    print(loaded.provenance.render())
    print(loaded.report.render())
    return 0 if loaded.ok else 1


def cmd_validate(args: argparse.Namespace) -> int:
    from .data.validate import validate

    settings = _settings(args)
    loaded = _load_bars(args, settings)
    report = validate(loaded.frame, check_fresh=args.check_fresh)
    print(report.render())

    if args.cross_provider:
        from .data.loader import load_two_and_compare

        print("\ncross-provider check:")
        _, _, cross = load_two_and_compare(
            settings.symbol, primary=args.cross_provider[0], secondary=args.cross_provider[1],
            cache_dir=settings.cache_dir, offline=args.offline,
        )
        print(cross.render())
        return 0 if (report.ok and cross.ok) else 1
    return 0 if report.ok else 1


def cmd_study(args: argparse.Namespace) -> int:
    from .research.report import render_console, write_report
    from .research.study import run_study

    settings = _settings(args)
    loaded = _load_bars(args, settings)
    print(loaded.provenance.render(), file=sys.stderr)

    result = run_study(
        loaded.frame,
        symbol=settings.symbol,
        provenance=f"{loaded.provenance.provider} ({loaded.provenance.adjustment})",
        validation=loaded.report,
        initial_capital=settings.initial_capital,
        n_variants_examined=args.variants,
        bootstrap_resamples=args.resamples,
    )
    print(render_console(result))
    paths = write_report(result, args.out or settings.reports_dir)
    print("\nwrote:\n  " + "\n  ".join(str(p) for p in paths.values()))
    return {"PASS": 0, "WARN": 0, "FAIL": 2}[result.reality.verdict.value]


def cmd_backtest(args: argparse.Namespace) -> int:
    from .research import metrics as M
    from .research.costs import scenario
    from .research.engine import BacktestConfig, simulate

    settings = _settings(args)
    loaded = _load_bars(args, settings)
    cfg = BacktestConfig(
        mode=args.mode,
        cost=scenario(args.cost),
        initial_capital=settings.initial_capital,
        leverage=args.leverage,
    )
    result = simulate(loaded.frame, cfg)
    met = M.compute(result.returns, equity=result.equity)
    print(cfg.describe())
    print(result.summary_line())
    print()
    for k, v in met.to_dict().items():
        print(f"  {k:<28} {v:>18,.6f}" if isinstance(v, float) else f"  {k:<28} {v:>18}")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    from .live.preflight import run_preflight

    settings = _settings(args)
    bars = None
    try:
        bars = _load_bars(args, settings, check_fresh=True).frame
    except Exception as exc:
        print(f"note: could not load bars ({exc})", file=sys.stderr)
    broker = _broker(settings, bars)
    report = run_preflight(settings, broker, _calendar(broker), probe_orders=args.probe_orders)
    print(report.render())
    return 0 if report.ok else 1


def cmd_paper(args: argparse.Namespace) -> int:
    from .live.audit import AuditLog, setup_logging
    from .live.runner import OvernightBot
    from .live.scheduler import run
    from .live.state import InstanceLock

    settings = _settings(args, dry_run=args.dry_run)
    settings.ensure_dirs()
    setup_logging(settings.log_level, settings.log_dir)

    try:
        lock = InstanceLock(Path(settings.state_dir) / ".lock")
        lock.acquire()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    bars = None
    try:
        bars = _load_bars(args, settings, check_fresh=True).frame
    except Exception as exc:
        print(f"warning: no price history available ({exc}); price-sanity guards will be inactive",
              file=sys.stderr)

    broker = _broker(settings, bars)
    bot = OvernightBot(
        settings, broker, calendar=_calendar(broker), bars=bars,
        audit=AuditLog(Path(settings.log_dir) / "audit.jsonl"),
    )
    run(bot, settings=settings, max_iterations=args.max_iterations)
    return 2 if bot.state.halted else 0


def cmd_tick(args: argparse.Namespace) -> int:
    from .live.audit import AuditLog, setup_logging
    from .live.runner import OvernightBot
    from .live.state import InstanceLock

    settings = _settings(args, dry_run=args.dry_run)
    settings.ensure_dirs()
    setup_logging(settings.log_level, settings.log_dir)

    try:
        lock = InstanceLock(Path(settings.state_dir) / ".lock")
        lock.acquire()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        bars = None
        try:
            bars = _load_bars(args, settings, check_fresh=True).frame
        except Exception:
            pass
        broker = _broker(settings, bars)
        bot = OvernightBot(settings, broker, calendar=_calendar(broker), bars=bars,
                           audit=AuditLog(Path(settings.log_dir) / "audit.jsonl"))
        for action in bot.tick():
            print(action)
        return 2 if bot.state.halted else 0
    finally:
        lock.release()


def cmd_status(args: argparse.Namespace) -> int:
    from .live.state import load as load_state

    settings = _settings(args)
    state = load_state(Path(settings.state_dir) / "state.json", symbol=settings.symbol)
    payload = {
        "symbol": state.symbol,
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "equity_peak": state.equity_peak,
        "consecutive_losses": state.consecutive_losses,
        "open_positions": [
            {"trade_date": t.trade_date, "phase": t.phase.value, "qty": t.entry.filled_qty}
            for t in state.open_trades()
        ],
        "trades_tracked": len(state.trades),
        "realized_pnl": sum(t.realized_pnl or 0.0 for t in state.trades.values()),
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    from .live.risk import engage_kill_switch

    settings = _settings(args)
    path = engage_kill_switch(settings.kill_switch_file, args.reason)
    print(f"kill switch engaged: {path}\nthe bot will not open any new position until it is removed")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from .live.risk import release_kill_switch
    from .live.state import load as load_state
    from .live.state import save as save_state

    settings = _settings(args)
    removed = release_kill_switch(settings.kill_switch_file)
    print("kill switch removed" if removed else "no kill switch was present")

    if args.clear_halt:
        state_path = Path(settings.state_dir) / "state.json"
        state = load_state(state_path, symbol=settings.symbol)
        if state.halted:
            print(f"clearing halt: {state.halt_reason}")
            state.resume()
            save_state(state, state_path)
        else:
            print("state was not halted")
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="micronalgo", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="offline end-to-end demo on synthetic data")
    d.add_argument("--sessions", type=int, default=7800)
    d.add_argument("--seed", type=int, default=1994)
    d.add_argument("--resamples", type=int, default=400)
    d.add_argument("--out", default=None)
    d.set_defaults(func=cmd_demo)

    for name, fn, helptext in (
        ("fetch", cmd_fetch, "download and cache the price history"),
        ("validate", cmd_validate, "check a series for the defects that matter"),
        ("study", cmd_study, "the full research report"),
        ("backtest", cmd_backtest, "run one configuration"),
    ):
        s = sub.add_parser(name, help=helptext)
        _add_common(s)
        s.add_argument("--provider", action="append",
                       help="repeatable, in preference order: stooq yahoo tiingo alpaca csv:/path.csv")
        s.add_argument("--start", default=None, help="YYYY-MM-DD")
        s.add_argument("--end", default=None, help="YYYY-MM-DD")
        s.add_argument("--refresh", action="store_true", help="ignore the cache")
        s.add_argument("--offline", action="store_true", help="cache only, never touch the network")
        s.set_defaults(func=fn)

    v = next(a for a in sub.choices.values() if a.get_default("func") is cmd_validate)
    v.add_argument("--check-fresh", action="store_true")
    v.add_argument("--cross-provider", nargs=2, metavar=("PRIMARY", "SECONDARY"),
                   help="compare two providers' overnight series")

    st = next(a for a in sub.choices.values() if a.get_default("func") is cmd_study)
    st.add_argument("--out", default=None)
    st.add_argument("--resamples", type=int, default=2000)
    st.add_argument("--variants", type=int, default=1,
                    help="how many configurations you examined in total; feeds the deflated Sharpe")

    b = next(a for a in sub.choices.values() if a.get_default("func") is cmd_backtest)
    b.add_argument("--mode", default="overnight",
                   choices=["overnight", "intraday", "short_intraday", "buyhold",
                            "overnight_plus_short_intraday"])
    b.add_argument("--cost", default="auction-retail")
    b.add_argument("--leverage", type=float, default=1.0)

    pf = sub.add_parser("preflight", help="verify every runtime assumption against the broker")
    _add_common(pf)
    pf.add_argument("--provider", action="append")
    pf.add_argument("--start", default=None)
    pf.add_argument("--end", default=None)
    pf.add_argument("--refresh", action="store_true")
    pf.add_argument("--offline", action="store_true")
    pf.add_argument("--probe-orders", action="store_true",
                    help="submit and cancel a 1-share auction order to prove the venue accepts it")
    pf.set_defaults(func=cmd_preflight)

    for name, fn, helptext in (("paper", cmd_paper, "run the bot"),
                               ("tick", cmd_tick, "one idempotent tick, for cron")):
        s = sub.add_parser(name, help=helptext)
        _add_common(s)
        s.add_argument("--provider", action="append")
        s.add_argument("--start", default=None)
        s.add_argument("--end", default=None)
        s.add_argument("--refresh", action="store_true")
        s.add_argument("--offline", action="store_true")
        group = s.add_mutually_exclusive_group()
        group.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                           help="log intended orders without sending them")
        group.add_argument("--live", dest="dry_run", action="store_false",
                           help="actually send orders (still paper unless the base URL says otherwise)")
        if name == "paper":
            s.add_argument("--max-iterations", type=int, default=None)
        s.set_defaults(func=fn)

    stt = sub.add_parser("status", help="what the bot currently believes")
    _add_common(stt)
    stt.set_defaults(func=cmd_status)

    k = sub.add_parser("kill", help="engage the kill switch")
    _add_common(k)
    k.add_argument("--reason", default="manual")
    k.set_defaults(func=cmd_kill)

    rs = sub.add_parser("resume", help="release the kill switch")
    _add_common(rs)
    rs.add_argument("--clear-halt", action="store_true", help="also clear a halt recorded in the state file")
    rs.set_defaults(func=cmd_resume)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
