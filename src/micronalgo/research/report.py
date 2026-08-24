"""Rendering: console text and a self-contained HTML report.

The HTML embeds its charts as base64 PNGs so the file can be mailed, archived or
opened offline years later and still show the same thing. No CDN, no external
assets, no JavaScript.

Report order is deliberate. The reality check comes **before** the headline
equity curve, because the headline number for this strategy is the part most
likely to mislead and the least likely to be read critically.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import io
from pathlib import Path

import numpy as np
import pandas as pd

from .study import StudyResult

_VERDICT_COLOR = {"PASS": "#1a7f37", "WARN": "#9a6700", "FAIL": "#b42318"}


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #

def _fmt_pct(x: float, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x * 100:,.{digits}f}%"


def render_console(r: StudyResult, *, width: int = 100) -> str:
    line = "=" * width
    thin = "-" * width
    out: list[str] = []

    out.append(line)
    out.append(f" {r.symbol}  OVERNIGHT vs INTRADAY  |  {r.start} .. {r.end}  |  {r.n_sessions:,} sessions")
    if r.provenance:
        out.append(f" data: {r.provenance}")
    out.append(line)

    out.append("")
    out.append(" THE CLAIM, CHECKED")
    out.append(thin)
    h = r.headline
    out.append(f"   buy every CLOSE, sell every OPEN   (overnight) : {_fmt_pct(h['overnight'], 1):>20}")
    out.append(f"   buy every OPEN,  sell every CLOSE  (intraday)  : {_fmt_pct(h['intraday'], 1):>20}")
    out.append(f"   buy and hold                        (baseline) : {_fmt_pct(h['buyhold'], 1):>20}")
    out.append(f"   decomposition identity error                   : {h['identity_error']:>20.2e}")
    out.append("   (all three are gross of costs -- see the next table, which is the number that matters)")

    out.append("")
    out.append(" AFTER COSTS")
    out.append(thin)
    tbl = r.by_scenario[["total_return", "cagr", "sharpe", "max_drawdown", "cost_per_trade_bps"]].copy()
    tbl.columns = ["total", "CAGR", "Sharpe", "maxDD", "cost bps/RT"]
    out.append(_indent(tbl.to_string(float_format=lambda x: f"{x:,.4f}"), 3))
    out.append("")
    out.append("   The whole strategy lives or dies on execution: a market-on-close buy and a")
    out.append("   market-on-open sell transact AT the auction print, which is the same price this")
    out.append("   backtest uses. Cross a spread instead and 252 round trips a year erase everything.")

    out.append("")
    out.append(" REALITY CHECK")
    out.append(thin)
    for c in r.reality.criteria:
        out.append(_indent(str(c), 3))
    out.append("")
    out.append(f"   VERDICT: {r.reality.verdict.value}")
    out.append(_indent(_wrap(r.reality.summary, width - 6), 3))

    out.append("")
    out.append(" WHY EACH LINE MATTERS")
    out.append(thin)
    for c in r.reality.criteria:
        out.append(f"   {c.name}")
        out.append(_indent(_wrap(c.explanation, width - 8), 5))

    out.append("")
    out.append(" BY REGIME  (the decay test)")
    out.append(thin)
    if not r.subperiods_regime.empty:
        out.append(_indent(
            r.subperiods_regime[["n", "total_return", "cagr", "mean_bps", "sharpe", "hit_rate"]]
            .to_string(float_format=lambda x: f"{x:,.4f}"), 3))

    out.append("")
    out.append(" EFFECTIVE SPREAD BY ERA  (is the 'edge' bigger than the bid-ask bounce?)")
    out.append(thin)
    if not r.spread_by_era.empty:
        out.append(_indent(r.spread_by_era.to_string(float_format=lambda x: f"{x:,.2f}"), 3))
    out.append("")
    out.append("   A close printed on the bid and an open printed on the ask manufacture a full")
    out.append("   effective spread of fake overnight return every session. Before decimalisation")
    out.append("   in 2001 the tick alone was 1/8 or 1/16 of a dollar, so the early history cannot")
    out.append("   distinguish the two explanations at all.")

    out.append("")
    out.append(" ANNUALISED DRAG OF A GIVEN ROUND-TRIP COST  (252 trades/yr)")
    out.append(thin)
    out.append(_indent(r.cost_drag[["annual_drag_pct"]].to_string(float_format=lambda x: f"{x:,.2f}"), 3))

    out.append("")
    out.append(" CONCENTRATION  (total return after deleting the best N sessions)")
    out.append(thin)
    out.append(_indent(r.drop_best.to_string(float_format=lambda x: f"{x:,.4f}"), 3))

    out.append("")
    out.append(" WORST OVERNIGHT GAPS  (held in full, no stop is possible)")
    out.append(thin)
    out.append(_indent(r.worst_gaps.to_string(float_format=lambda x: f"{x:,.4f}"), 3))

    out.append("")
    out.append(" DAY OF WEEK  (Monday spans the weekend: 3 calendar days, not 1)")
    out.append(thin)
    out.append(_indent(r.day_of_week.to_string(float_format=lambda x: f"{x:,.2f}"), 3))

    out.append("")
    out.append(" STATISTICS")
    out.append(thin)
    out.append(f"   stationary bootstrap mean : {r.bootstrap.observed * 1e4:.2f} bps  "
               f"95% CI [{r.bootstrap.ci_low * 1e4:.2f}, {r.bootstrap.ci_high * 1e4:.2f}]  "
               f"p={r.bootstrap.p_value_gt_zero:.4f}  block={r.bootstrap.block_length:.0f}")
    out.append(f"   overnight vs intraday     : diff {r.permutation['observed_diff'] * 1e4:.2f} bps  "
               f"permutation p={r.permutation['p_value']:.4f}")
    out.append(f"   break-even round trip     : {r.breakeven.breakeven_bps:.2f} bps  "
               f"(arithmetic {r.breakeven.arithmetic_mean * 1e4:.2f} - variance drag "
               f"{r.breakeven.variance_drag * 1e4:.2f})")

    out.append("")
    out.append(" MONTE CARLO  (block-resampled futures of equal length, after costs)")
    out.append(thin)
    out.append(_indent(r.monte_carlo.to_string(float_format=lambda x: f"{x:,.4f}"), 3))

    if r.validation is not None and (r.validation.errors or r.validation.warnings):
        out.append("")
        out.append(" DATA VALIDATION")
        out.append(thin)
        for f in r.validation.findings:
            if f.severity.value != "INFO":
                out.append(_indent(str(f), 3))

    out.append("")
    out.append(line)
    return "\n".join(out)


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + ln for ln in text.splitlines())


def _wrap(text: str, width: int) -> str:
    import textwrap
    return "\n".join(textwrap.wrap(text, width))


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #

def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    import matplotlib.pyplot as plt
    plt.close(fig)
    return base64.b64encode(buf.read()).decode("ascii")


def build_charts(r: StudyResult) -> dict[str, str]:
    """Base64 PNGs keyed by name. Returns {} if matplotlib is unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return {}

    charts: dict[str, str] = {}
    frame = r.decomposition.frame

    # 1. The three curves, log scale -- the picture that started all this.
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for name, series, colour in (
        ("overnight (close -> open)", frame["r_on"], "#1f77b4"),
        ("intraday (open -> close)", frame["r_id"], "#d62728"),
        ("buy & hold", frame["r_cc"], "#7f7f7f"),
    ):
        curve = (1.0 + series.fillna(0.0)).cumprod()
        ax.plot(curve.index, curve.clip(lower=1e-6), label=name, color=colour, lw=1.3)
    ax.set_yscale("log")
    ax.set_ylabel("growth of 1 (log scale)")
    ax.set_title(f"{r.symbol}: where the return actually happens (gross of costs)")
    ax.legend(loc="upper left", frameon=False)
    ax.grid(alpha=0.25, which="both")
    charts["decomposition"] = _png(fig)

    # 2. After-cost equity per scenario.
    fig, ax = plt.subplots(figsize=(11, 5.0))
    for name, res in r.results.items():
        if name in ("intraday", "buyhold") or res.equity.empty:
            continue
        ax.plot(res.equity.index, res.equity.clip(lower=1e-3), label=name, lw=1.2)
    ax.set_yscale("log")
    ax.set_ylabel("equity (log scale)")
    ax.set_title("Overnight strategy under different execution assumptions")
    ax.legend(loc="upper left", frameon=False, fontsize=8)
    ax.grid(alpha=0.25, which="both")
    charts["scenarios"] = _png(fig)

    # 3. Rolling 1-year Sharpe -- the decay picture.
    if not r.rolling.empty:
        fig, ax = plt.subplots(figsize=(11, 3.6))
        ax.plot(r.rolling.index, r.rolling["sharpe"], color="#1f77b4", lw=1.1)
        ax.axhline(0, color="#b42318", lw=0.9, ls="--")
        ax.set_title("Rolling 252-session Sharpe of the overnight return")
        ax.grid(alpha=0.25)
        charts["rolling"] = _png(fig)

    # 4. Distribution with the left tail marked.
    fig, ax = plt.subplots(figsize=(11, 3.6))
    data = frame["r_on"].dropna() * 100
    ax.hist(data, bins=160, color="#1f77b4", alpha=0.85)
    ax.axvline(float(data.mean()), color="#1a7f37", lw=1.2, label=f"mean {data.mean():.3f}%")
    ax.axvline(float(data.quantile(0.01)), color="#b42318", lw=1.2, ls="--",
               label=f"1st pct {data.quantile(0.01):.2f}%")
    ax.set_yscale("log")
    ax.set_xlabel("overnight return (%)")
    ax.set_title("Overnight return distribution -- note the log-scaled left tail")
    ax.legend(frameon=False)
    charts["distribution"] = _png(fig)

    # 5. Drawdown of the primary scenario.
    primary = r.results.get("auction-retail")
    if primary is not None and not primary.equity.empty:
        eq = primary.equity
        dd = eq / eq.cummax() - 1.0
        fig, ax = plt.subplots(figsize=(11, 3.2))
        ax.fill_between(dd.index, dd * 100, 0, color="#b42318", alpha=0.55)
        ax.set_ylabel("drawdown (%)")
        ax.set_title("Drawdown, overnight strategy after retail auction costs")
        ax.grid(alpha=0.25)
        charts["drawdown"] = _png(fig)

    return charts


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

_CSS = """
:root { color-scheme: light dark;
  --bg:#ffffff; --fg:#1c2024; --muted:#5b6470; --line:#e3e6ea; --card:#f7f8fa; --accent:#1f6feb; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#0f1216; --fg:#e6e9ee; --muted:#9aa4b2; --line:#242a31; --card:#161b22; --accent:#589bff; } }
* { box-sizing:border-box; }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width:1080px; margin:0 auto; }
h1 { font-size:1.7rem; margin:0 0 .25rem; letter-spacing:-.01em; }
h2 { font-size:1.15rem; margin:2.5rem 0 .75rem; padding-bottom:.4rem; border-bottom:1px solid var(--line); }
h3 { font-size:.98rem; margin:1.4rem 0 .4rem; }
.sub { color:var(--muted); font-size:.9rem; margin-bottom:1.5rem; }
.verdict { padding:1rem 1.15rem; border-radius:10px; border:1px solid var(--line);
  background:var(--card); margin:1rem 0 1.5rem; }
.verdict .tag { display:inline-block; padding:.15rem .6rem; border-radius:999px; color:#fff;
  font-weight:600; font-size:.8rem; letter-spacing:.04em; }
table { border-collapse:collapse; width:100%; font-size:.86rem; margin:.5rem 0 1rem; }
th,td { padding:.4rem .6rem; text-align:right; border-bottom:1px solid var(--line); white-space:nowrap; }
th:first-child, td:first-child { text-align:left; }
thead th { color:var(--muted); font-weight:600; font-size:.8rem; text-transform:uppercase;
  letter-spacing:.04em; }
tbody tr:hover { background:var(--card); }
.scroll { overflow-x:auto; }
figure { margin:1rem 0 1.5rem; }
figure img { width:100%; height:auto; border:1px solid var(--line); border-radius:8px; background:#fff; }
figcaption { color:var(--muted); font-size:.83rem; margin-top:.4rem; }
.crit { display:grid; grid-template-columns:auto 1fr auto; gap:.5rem .9rem; align-items:baseline;
  padding:.55rem 0; border-bottom:1px solid var(--line); }
.crit .badge { font-weight:700; font-size:.75rem; letter-spacing:.05em; padding:.1rem .45rem;
  border-radius:4px; color:#fff; }
.crit .why { grid-column:2 / -1; color:var(--muted); font-size:.85rem; margin-top:.15rem; }
.crit .val { font-variant-numeric:tabular-nums; font-weight:600; }
.note { background:var(--card); border-left:3px solid var(--accent); padding:.8rem 1rem;
  border-radius:0 8px 8px 0; margin:1rem 0; font-size:.9rem; }
code { background:var(--card); padding:.1rem .35rem; border-radius:4px; font-size:.85em; }
footer { margin-top:3rem; padding-top:1rem; border-top:1px solid var(--line);
  color:var(--muted); font-size:.8rem; }
"""


def _table(df: pd.DataFrame, *, floats: int = 4) -> str:
    if df is None or df.empty:
        return "<p class='sub'>no data</p>"
    def fmt(v):
        if isinstance(v, float):
            return "n/a" if not np.isfinite(v) else f"{v:,.{floats}f}"
        return html.escape(str(v))
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = "".join(
        "<tr><td>" + html.escape(str(idx)) + "</td>"
        + "".join(f"<td>{fmt(v)}</td>" for v in row) + "</tr>"
        for idx, row in zip(df.index, df.to_numpy(), strict=True)
    )
    return f"<div class='scroll'><table><thead><tr><th></th>{head}</tr></thead><tbody>{rows}</tbody></table></div>"


def render_html(r: StudyResult, *, charts: dict[str, str] | None = None) -> str:
    charts = charts if charts is not None else build_charts(r)
    v = r.reality.verdict
    colour = _VERDICT_COLOR[v.value]

    crits = []
    for c in r.reality.criteria:
        cc = _VERDICT_COLOR[c.verdict.value]
        crits.append(
            f"<div class='crit'>"
            f"<span class='badge' style='background:{cc}'>{c.verdict.value}</span>"
            f"<span>{html.escape(c.name)}</span>"
            f"<span class='val'>{html.escape(c.value)}</span>"
            f"<span class='why'>{html.escape(c.explanation)} "
            f"<em>Threshold: {html.escape(c.threshold)}.</em></span>"
            f"</div>"
        )

    def fig(key: str, caption: str) -> str:
        if key not in charts:
            return ""
        return (
            f"<figure><img alt='{html.escape(caption)}' "
            f"src='data:image/png;base64,{charts[key]}'>"
            f"<figcaption>{html.escape(caption)}</figcaption></figure>"
        )

    h = r.headline
    headline_tbl = pd.DataFrame(
        {
            "total return": [h["overnight"], h["intraday"], h["buyhold"]],
        },
        index=["overnight (close -> open)", "intraday (open -> close)", "buy & hold"],
    )

    body = f"""
<div class="wrap">
<h1>{html.escape(r.symbol)} &mdash; overnight vs. intraday</h1>
<p class="sub">{r.start} to {r.end} &middot; {r.n_sessions:,} sessions &middot;
{html.escape(r.provenance or 'provenance not recorded')} &middot;
generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<div class="verdict">
  <span class="tag" style="background:{colour}">VERDICT: {v.value}</span>
  <p style="margin:.7rem 0 0">{html.escape(r.reality.summary)}</p>
</div>

<h2>Reality check</h2>
<p class="sub">Read this before the equity curve. The headline number is the part most likely to mislead.</p>
{''.join(crits)}

<h2>The claim, checked</h2>
{_table(headline_tbl)}
<div class="note">These three are <strong>gross of costs</strong> and multiply out exactly:
(1+overnight) &times; (1+intraday) = (1+buy&amp;hold), verified to
{h['identity_error']:.2e}. The number that matters is the after-cost table below.</div>
{fig('decomposition', 'Growth of 1 in each window, log scale. The gap between the blue and red lines is the entire phenomenon.')}

<h2>After costs</h2>
{_table(r.by_scenario[['total_return','cagr','sharpe','max_drawdown','cost_per_trade_bps']])}
<div class="note">A market-on-close buy and a market-on-open sell fill <em>at</em> the auction print &mdash;
the same prices this backtest uses &mdash; so a retail-sized order does not cross the spread. That single
structural fact is why the strategy is not obviously dead, and it is also the assumption most worth
attacking: the <code>cross-spread-era</code> row shows what happens when it fails.</div>
{fig('scenarios', 'The same strategy under different execution assumptions.')}

<h2>Effective spread by era</h2>
<p class="sub">Is the measured edge bigger than the bid-ask bounce that could fake it?</p>
{_table(r.spread_by_era, floats=2)}
<div class="note">A close printed on the bid and an open printed on the ask manufacture a full
effective spread of overnight "return" every session, with no economics behind it. Before
decimalisation in April 2001 the tick size alone was 1/8 or 1/16 of a dollar, so the early history
cannot separate the two explanations. The spread here is estimated from daily highs and lows
(Corwin-Schultz), which runs high on volatile stocks &mdash; it errs towards flagging.</div>

<h2>Annualised drag of a given round-trip cost</h2>
<p class="sub">252 round trips a year compound small costs into large ones.</p>
{_table(r.cost_drag[['annual_drag_pct']], floats=2)}

<h2>Has it decayed?</h2>
{_table(r.subperiods_regime[['n','total_return','cagr','mean_bps','sharpe','hit_rate']])}
{fig('rolling', 'Rolling one-year Sharpe of the overnight return. A downward trend is decay, not noise.')}
<h3>By decade</h3>
{_table(r.subperiods_decade[['n','total_return','cagr','mean_bps','sharpe','hit_rate']])}

<h2>Is it a handful of sessions?</h2>
{_table(r.drop_best)}
{fig('distribution', 'Overnight return distribution. The log-scaled y-axis makes the tail honest.')}

<h2>The risk actually borne</h2>
<p class="sub">You hold every earnings gap overnight, and there is no stop-loss while the market is shut.</p>
{_table(r.worst_gaps)}
{fig('drawdown', 'Drawdown after retail auction costs. This is the number that decides whether a human stays in the trade.')}

<h2>Monte Carlo</h2>
<p class="sub">Block-resampled alternative histories of the same length, after costs.</p>
{_table(r.monte_carlo)}

<h2>Statistics</h2>
{_table(pd.DataFrame({
    'value': [
        f"{r.bootstrap.observed*1e4:.2f} bps",
        f"[{r.bootstrap.ci_low*1e4:.2f}, {r.bootstrap.ci_high*1e4:.2f}] bps",
        f"{r.bootstrap.p_value_gt_zero:.4f}",
        f"{r.breakeven.breakeven_bps:.2f} bps",
        f"{r.breakeven.variance_drag*1e4:.2f} bps",
        f"{r.permutation['p_value']:.4f}",
    ]}, index=[
        'bootstrap mean (overnight)', 'bootstrap 95% CI', 'bootstrap p-value',
        'break-even round-trip cost', 'variance drag per session',
        'overnight-vs-intraday permutation p']))}

<h2>Day of week</h2>
<p class="sub">Monday's overnight spans three calendar days. If the premium were compensation for
calendar-time risk it should scale with that. It generally does not.</p>
{_table(r.day_of_week, floats=2)}

<footer>
Generated by micronalgo. This is research output, not investment advice. Past behaviour of a price
series is not a forecast, single-stock strategies carry idiosyncratic risk that diversification would
remove, and every number here depends on the data and cost assumptions stated above.
</footer>
</div>
"""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(r.symbol)} overnight study</title>"
        f"<style>{_CSS}</style></head><body>{body}</body></html>"
    )


def write_report(r: StudyResult, out_dir: Path | str, *, stem: str = "") -> dict[str, Path]:
    """Write ``<stem>.txt``, ``<stem>.html`` and ``<stem>.json``; returns the paths."""
    import json

    from .study import to_dict

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or f"{r.symbol.lower()}_overnight_{dt.date.today().isoformat()}"

    paths = {
        "txt": out_dir / f"{stem}.txt",
        "html": out_dir / f"{stem}.html",
        "json": out_dir / f"{stem}.json",
    }
    paths["txt"].write_text(render_console(r), encoding="utf-8")
    paths["html"].write_text(render_html(r), encoding="utf-8")
    paths["json"].write_text(json.dumps(to_dict(r), indent=2), encoding="utf-8")
    return paths
