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
    # None means "the flag was not given" -- e.g. the --dry-run/--live pair
    # defaults to None so that plain `micronalgo paper` falls through to the
    # environment/default. Passing that None into pydantic is a ValidationError,
    # which made the unflagged invocation crash until a test caught it.
    kw = {k: v for k, v in overrides.items() if v is not None}
    for name in ("symbol", "cache_dir", "log_level"):
        val = getattr(args, name, None)
        if val:
            kw[name] = val
    return load_settings(**kw)


def _progress(args: argparse.Namespace):
    """Progress lines go to stderr, so `micronalgo study > file` stays clean."""
    import time

    if getattr(args, "quiet", False):
        return lambda _msg: None
    start = time.monotonic()

    def say(message: str) -> None:
        print(f"  [{time.monotonic() - start:5.1f}s] {message}", file=sys.stderr, flush=True)

    return say


def _load_bars(args: argparse.Namespace, settings: Settings, *, check_fresh: bool = False,
               on_progress=None):
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
        on_progress=on_progress,
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
    say = _progress(args)
    loaded = _load_bars(args, settings, on_progress=say)
    print(loaded.provenance.render(), file=sys.stderr)

    result = run_study(
        loaded.frame,
        symbol=settings.symbol,
        provenance=f"{loaded.provenance.provider} ({loaded.provenance.adjustment})",
        validation=loaded.report,
        initial_capital=settings.initial_capital,
        n_variants_examined=args.variants,
        bootstrap_resamples=args.resamples,
        on_progress=say,
    )
    print(render_console(result))
    paths = write_report(result, args.out or settings.reports_dir, on_progress=say)
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


def cmd_walkforward(args: argparse.Namespace) -> int:
    from .research.costs import scenario
    from .research.engine import BacktestConfig
    from .research.walkforward import walk_forward

    settings = _settings(args)
    loaded = _load_bars(args, settings)
    print(loaded.provenance.render(), file=sys.stderr)

    result = walk_forward(
        loaded.frame,
        config=BacktestConfig(cost=scenario(args.cost), initial_capital=settings.initial_capital),
        train_sessions=args.train,
        test_sessions=args.test,
        score=args.score,
    )
    print(result.fold_table().to_string(float_format=lambda x: f"{x:,.2f}"))
    print()
    print(result.verdict())
    print()
    print(f"deflated Sharpe (out-of-sample, {len(result.candidate_names)} candidates): "
          f"{result.deflated_sharpe_oos():.3f}")
    print()
    print("Remember what this is: the ONLY curve a deployment decision may be read from is the")
    print("out-of-sample one above. In-sample tables are how strategies get overfitted.")
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
    import threading

    from .live.audit import AuditLog, get_logger, setup_logging
    from .live.runner import OvernightBot
    from .live.scheduler import run
    from .live.state import InstanceLock

    settings = _settings(args, dry_run=args.dry_run)
    settings.ensure_dirs()
    setup_logging(settings.log_level, settings.log_dir)
    logger = get_logger("cli")

    try:
        lock = InstanceLock(Path(settings.state_dir) / ".lock")
        lock.acquire()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Halt guard, and the exit code matters: launchd's KeepAlive/SuccessfulExit=false
    # restarts NON-ZERO exits (that is what keeps the bot alive through crashes).
    # A halt mid-run exits 2, launchd restarts once, and this check then exits 0 --
    # a *successful* exit -- so launchd leaves the process down until a human runs
    # `micronalgo resume --clear-halt`. Without this, a halted bot would be
    # restarted every ThrottleInterval seconds, alert-spamming forever.
    from .live.state import load as _load_state

    pre_state = _load_state(Path(settings.state_dir) / "state.json", symbol=settings.symbol)
    if pre_state.halted:
        print(
            f"bot is HALTED: {pre_state.halt_reason}\n"
            "Refusing to start. Investigate, then run: micronalgo resume --clear-halt",
            file=sys.stderr,
        )
        return 0

    bars = None
    try:
        bars = _load_bars(args, settings, check_fresh=True).frame
    except Exception as exc:
        print(f"warning: no price history available ({exc}); price-sanity guards will be inactive",
              file=sys.stderr)

    broker = _broker(settings, bars)

    # ------------------------------------------------------------- streaming
    # The websocket streams are accelerators, never dependencies: order events
    # wake the idempotent tick immediately, live trades keep the sizing
    # reference fresh. Any failure here degrades to plain polling, loudly.
    wake: threading.Event | None = None
    price_feed = None
    workers = []
    want_stream = getattr(args, "stream", True) and settings.broker == "alpaca"
    if want_stream:
        try:
            from .live.stream import LivePrice, market_data_worker, trade_updates_worker

            wake = threading.Event()
            live_price = LivePrice()
            workers.append(
                market_data_worker(
                    settings.alpaca_key_id, settings.alpaca_secret_key,
                    settings.symbol, live_price, feed=settings.alpaca_data_feed,
                )
            )
            workers.append(
                trade_updates_worker(
                    settings.alpaca_key_id, settings.alpaca_secret_key,
                    settings.symbol, wake=wake, base_url=settings.alpaca_base_url,
                )
            )
            for w in workers:
                w.start()
            price_feed = live_price.get
            logger.info("streaming enabled: live trades (%s feed) + order events", settings.alpaca_data_feed)
        except Exception as exc:
            logger.warning("streaming unavailable (%s); continuing with REST polling only", exc)
            wake, price_feed, workers = None, None, []
    elif settings.broker == "alpaca":
        logger.info("streaming disabled by --no-stream; REST polling only")

    bot = OvernightBot(
        settings, broker, calendar=_calendar(broker), bars=bars,
        audit=AuditLog(Path(settings.log_dir) / "audit.jsonl"),
        price_feed=price_feed,
    )
    try:
        run(bot, settings=settings, max_iterations=args.max_iterations, wake=wake)
    finally:
        for w in workers:
            w.stop()
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


def _status_payload(settings: Settings) -> dict:
    from .live.risk import kill_switch_active
    from .live.state import load as load_state

    state = load_state(Path(settings.state_dir) / "state.json", symbol=settings.symbol)
    return {
        "symbol": state.symbol,
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "kill_switch": kill_switch_active(settings.kill_switch_file),
        "equity_peak": state.equity_peak,
        "consecutive_losses": state.consecutive_losses,
        "open_positions": [
            {"trade_date": t.trade_date, "phase": t.phase.value, "qty": t.entry.filled_qty}
            for t in state.open_trades()
        ],
        "trades_tracked": len(state.trades),
        "realized_pnl": sum(t.realized_pnl or 0.0 for t in state.trades.values()),
    }


def cmd_status(args: argparse.Namespace) -> int:
    import datetime as dt
    import time

    settings = _settings(args)
    if not getattr(args, "watch", False):
        print(json.dumps(_status_payload(settings), indent=2))
        return 0

    # Watch mode: a lightweight live view for a terminal left open on the Mac.
    # Reads only local files, so it never competes with the bot for API budget.
    audit_path = Path(settings.log_dir) / "audit.jsonl"
    iterations = getattr(args, "_watch_iterations", None)  # test hook
    count = 0
    try:
        while True:
            payload = _status_payload(settings)
            lines = ["\x1b[2J\x1b[H" if iterations is None else "",
                     f"micronalgo status  {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
                     "-" * 60]
            state_word = "HALTED" if payload["halted"] else ("KILL SWITCH" if payload["kill_switch"] else "running")
            lines.append(f"  state         : {state_word}"
                         + (f"  ({payload['halt_reason']})" if payload["halt_reason"] else ""))
            if payload["open_positions"]:
                for pos in payload["open_positions"]:
                    lines.append(f"  position      : {pos['qty']:g} {payload['symbol']} "
                                 f"({pos['phase']}, entered {pos['trade_date']})")
            else:
                lines.append(f"  position      : flat ({payload['symbol']})")
            lines.append(f"  realized pnl  : ${payload['realized_pnl']:,.2f} "
                         f"over {payload['trades_tracked']} tracked trades")
            lines.append(f"  loss streak   : {payload['consecutive_losses']}")
            if audit_path.exists():
                # Tail only: the audit log grows without bound, and this view
                # refreshes every few seconds for as long as a terminal is open.
                with audit_path.open("rb") as fh:
                    fh.seek(0, 2)
                    fh.seek(max(fh.tell() - 16_384, 0))
                    tail = fh.read().decode("utf-8", errors="replace").splitlines()[-5:]
                lines.append("")
                lines.append("  last audit events:")
                for raw in tail:
                    try:
                        rec = json.loads(raw)
                        lines.append(f"    {rec.get('ts_ny', '')[:19]}  {rec.get('event', '?')}")
                    except json.JSONDecodeError:
                        continue
            print("\n".join(lines), flush=True)
            count += 1
            if iterations is not None and count >= iterations:
                break
            time.sleep(max(args.interval, 1))
    except KeyboardInterrupt:
        pass
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
    st.add_argument("--quiet", action="store_true", help="no progress lines on stderr")

    b = next(a for a in sub.choices.values() if a.get_default("func") is cmd_backtest)
    b.add_argument("--mode", default="overnight",
                   choices=["overnight", "intraday", "short_intraday", "buyhold",
                            "overnight_plus_short_intraday"])
    b.add_argument("--cost", default="auction-retail")
    b.add_argument("--leverage", type=float, default=1.0)

    wf = sub.add_parser("walkforward", help="evaluate filter overlays strictly out-of-sample")
    _add_common(wf)
    wf.add_argument("--provider", action="append")
    wf.add_argument("--start", default=None)
    wf.add_argument("--end", default=None)
    wf.add_argument("--refresh", action="store_true")
    wf.add_argument("--offline", action="store_true")
    wf.add_argument("--cost", default="auction-retail")
    wf.add_argument("--train", type=int, default=756, help="training sessions per fold (default 3y)")
    wf.add_argument("--test", type=int, default=252, help="test sessions per fold (default 1y)")
    wf.add_argument("--score", default="geometric_mean", choices=["geometric_mean", "calmar"],
                    help="what each fold selects on: raw compound growth, or growth per unit of "
                         "drawdown. Use calmar when the problem is holdability, not edge.")
    wf.set_defaults(func=cmd_walkforward)

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
            s.add_argument("--no-stream", dest="stream", action="store_false", default=True,
                           help="disable the websocket streams and rely on REST polling only")
        s.set_defaults(func=fn)

    stt = sub.add_parser("status", help="what the bot currently believes")
    _add_common(stt)
    stt.add_argument("--watch", action="store_true", help="live view; refresh until Ctrl-C")
    stt.add_argument("--interval", type=int, default=10, help="watch refresh seconds")
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
