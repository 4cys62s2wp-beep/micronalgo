"""Schema, validation and providers -- all offline, against recorded payloads."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from micronalgo.data.loader import Provenance, load, read_cache, write_cache
from micronalgo.data.providers import ProviderError
from micronalgo.data.providers.csv_local import CsvProvider
from micronalgo.data.providers.http_sources import (
    parse_alpaca_bars,
    parse_stooq_csv,
    parse_tiingo,
    parse_yahoo_chart,
)
from micronalgo.data.schema import SchemaError, coerce, validate_schema
from micronalgo.data.synthetic import break_open_adjustment, random_walk
from micronalgo.data.validate import (
    Severity,
    ValidationReport,
    check_adjustment,
    check_frozen_rows,
    check_prices,
    check_sessions,
    check_split_artifacts,
    check_staleness,
    validate,
)


# --------------------------------------------------------------------- schema
def test_coerce_fills_raw_and_factor():
    df = pd.DataFrame(
        {"open": [1.0, 2.0], "high": [2.0, 3.0], "low": [0.5, 1.0], "close": [1.5, 2.5]},
        index=pd.to_datetime(["2024-01-03", "2024-01-02"]),
    )
    out = coerce(df)
    validate_schema(out)
    assert out.index.is_monotonic_increasing
    assert (out["raw_close"] == out["close"]).all()
    assert (out["adj_factor"] == 1.0).all()


def test_coerce_strips_timezone_without_shifting_the_date():
    """A tz-aware UTC index rolls half the history back a day if converted naively."""
    idx = pd.to_datetime(["2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z"])
    df = pd.DataFrame({"open": [1.0, 2.0], "high": [2.0, 3.0], "low": [0.5, 1.0], "close": [1.5, 2.5]},
                      index=idx)
    out = coerce(df)
    assert out.index.tz is None
    # 2024-01-02T00:00Z is 2024-01-01 19:00 New York -> the session date is the 1st.
    assert out.index[0].date() == dt.date(2024, 1, 1)


def test_validate_schema_rejects_duplicates_and_bad_dtypes(walk_bars):
    dupe = pd.concat([walk_bars, walk_bars.iloc[[5]]]).sort_index()
    with pytest.raises(SchemaError, match="duplicate"):
        validate_schema(dupe)
    wrong = walk_bars.copy()
    wrong["close"] = wrong["close"].astype("float32")
    with pytest.raises(SchemaError, match="float64"):
        validate_schema(wrong)


# ------------------------------------------------------------------ validation
def test_clean_series_passes(walk_bars):
    assert validate(walk_bars).ok


def test_open_left_unadjusted_is_caught(walk_bars):
    """The dangerous corruption: close-to-close still looks perfect."""
    broken = break_open_adjustment(walk_bars, on="2018-06-01", ratio=2.0)
    report = validate(broken)
    assert not report.ok
    checks = {f.check for f in report.errors}
    assert "adjustment.split_artifact" in checks or "prices.ohlc_range" in checks


def test_exdate_detector_fires_only_when_it_should(walk_bars):
    ex = walk_bars.index[walk_bars.index.searchsorted(pd.Timestamp("2018-06-01"))]
    mask = walk_bars.index < ex

    mis = walk_bars.copy()
    mis.loc[mask, "adj_factor"] = 0.5
    mis.loc[ex, "open"] = mis.loc[ex, "open"] * 2.0
    r = ValidationReport(n_rows=len(mis))
    check_adjustment(mis, r)
    assert any(f.check == "adjustment.exdate_gap" for f in r.errors)

    clean = walk_bars.copy()
    clean.loc[mask, "adj_factor"] = 0.5
    r2 = ValidationReport(n_rows=len(clean))
    check_adjustment(clean, r2)
    assert not any(f.check == "adjustment.exdate_gap" for f in r2.findings)


def test_unapplied_split_is_detected():
    bars = random_walk(600, seed=3, start="2018-01-02")
    ex_pos = 300
    bad = bars.copy()
    for c in ("open", "high", "low", "close"):
        bad.iloc[:ex_pos, bad.columns.get_loc(c)] *= 2.0
    r = ValidationReport(n_rows=len(bad))
    check_split_artifacts(bad, r)
    assert any(f.check == "adjustment.split_artifact" for f in r.errors)


def test_ohlc_range_violation_is_an_error(walk_bars):
    bad = walk_bars.copy()
    idx = bad.index[:10]
    bad.loc[idx, "high"] = bad.loc[idx, "low"] * 0.5
    r = ValidationReport(n_rows=len(bad))
    check_prices(bad, r)
    assert any(f.check == "prices.ohlc_range" for f in r.errors)


def test_missing_sessions_are_reported(walk_bars):
    gapped = walk_bars.drop(walk_bars.index[100:200])
    r = ValidationReport(n_rows=len(gapped))
    check_sessions(gapped, r)
    assert any(f.check == "sessions.missing" for f in r.findings)


def test_frozen_feed_is_flagged(walk_bars):
    frozen = walk_bars.copy()
    row = frozen.iloc[50]
    for i in range(50, 60):
        frozen.iloc[i] = row
    r = ValidationReport(n_rows=len(frozen))
    check_frozen_rows(frozen, r)
    assert any(f.check == "quality.frozen" for f in r.findings)


def test_staleness_blocks_live_use(walk_bars):
    r = ValidationReport(n_rows=len(walk_bars))
    check_staleness(walk_bars, r, max_age_days=5, today=dt.date(2030, 1, 1))
    assert any(f.check == "freshness" and f.severity is Severity.ERROR for f in r.findings)


# ------------------------------------------------------------------- providers
STOOQ_CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2024-01-02,85.00,86.50,84.00,86.00,10000000\n"
    "2024-01-03,86.20,87.00,85.10,85.50,12000000\n"
)


def test_stooq_parser():
    df = parse_stooq_csv(STOOQ_CSV)
    assert len(df) == 2
    assert df["close"].iloc[-1] == 85.5
    assert (df["adj_factor"] == 1.0).all()


def test_stooq_rejects_an_html_error_page():
    """Stooq returns HTTP 200 with an HTML body when it throttles."""
    with pytest.raises(ProviderError, match="did not return a CSV"):
        parse_stooq_csv("<html><body>No data</body></html>")


def test_yahoo_applies_the_factor_to_open_and_close_alike():
    """Applying Adj Close next to a raw Open is how this study is usually ruined."""
    payload = {
        "chart": {"result": [{
            "timestamp": [1704207600, 1704294000],
            "indicators": {
                "quote": [{"open": [85.0, 86.2], "high": [86.5, 87.0],
                           "low": [84.0, 85.1], "close": [86.0, 85.5],
                           "volume": [1000, 1200]}],
                "adjclose": [{"adjclose": [43.0, 42.75]}],
            },
        }]}
    }
    df = parse_yahoo_chart(payload)
    assert df["adj_factor"].iloc[0] == pytest.approx(0.5)
    assert df["open"].iloc[0] == pytest.approx(42.5)
    assert df["close"].iloc[0] == pytest.approx(43.0)
    # the overnight ratio is unchanged by a uniform factor -- that is the point
    raw_ratio = 86.2 / 86.0
    adj_ratio = df["open"].iloc[1] / df["close"].iloc[0]
    assert adj_ratio == pytest.approx(raw_ratio, rel=1e-12)


def test_yahoo_reports_a_useful_error_on_a_bad_payload():
    with pytest.raises(ProviderError, match="unexpected yahoo payload"):
        parse_yahoo_chart({"chart": {"result": [], "error": "Not Found"}})


def test_tiingo_prefers_adjusted_and_keeps_raw():
    rows = [{"date": "2024-01-02T00:00:00.000Z", "open": 85.0, "high": 86.5, "low": 84.0,
             "close": 86.0, "volume": 1000, "adjOpen": 42.5, "adjHigh": 43.25,
             "adjLow": 42.0, "adjClose": 43.0, "adjVolume": 2000}]
    df = parse_tiingo(rows)
    assert df["close"].iloc[0] == 43.0 and df["raw_close"].iloc[0] == 86.0
    assert df["adj_factor"].iloc[0] == pytest.approx(0.5)


@pytest.mark.parametrize("shape", ["dict", "list"])
def test_alpaca_handles_both_response_shapes(shape):
    bars = [{"t": "2024-01-02T05:00:00Z", "o": 85.0, "h": 86.5, "l": 84.0, "c": 86.0, "v": 1000}]
    payload = {"bars": {"MU": bars}} if shape == "dict" else {"bars": bars}
    df = parse_alpaca_bars(payload, "MU")
    assert len(df) == 1 and df["close"].iloc[0] == 86.0


def test_csv_provider_roundtrip(tmp_path):
    path = tmp_path / "MU.csv"
    path.write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "2024-01-02,85.0,86.5,84.0,86.0,43.0,1000\n"
        "2024-01-03,86.2,87.0,85.1,85.5,42.75,1200\n"
    )
    fetched = CsvProvider(path).fetch("MU")
    assert len(fetched.frame) == 2
    assert fetched.frame["adj_factor"].iloc[0] == pytest.approx(0.5)
    assert fetched.adjustment.dividends


def test_csv_provider_reports_missing_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("Date,Price\n2024-01-02,85.0\n")
    with pytest.raises(ProviderError, match="missing columns"):
        CsvProvider(path).fetch("MU")


# ---------------------------------------------------------------------- cache
def test_cache_roundtrip_and_offline_load(tmp_path, walk_bars):
    prov = Provenance("MU", "stooq", "split-adj", "test",
                      dt.datetime.now(dt.timezone.utc).isoformat(),
                      len(walk_bars), str(walk_bars.index[0].date()), str(walk_bars.index[-1].date()))
    write_cache(walk_bars, prov, tmp_path)
    back, meta = read_cache("MU", "stooq", tmp_path)
    assert back.equals(walk_bars) and meta.provider == "stooq"

    loaded = load("MU", providers=("stooq",), cache_dir=tmp_path, offline=True)
    assert len(loaded.frame) == len(walk_bars)


def test_offline_miss_explains_the_csv_fallback(tmp_path):
    with pytest.raises(ProviderError, match="csv:/path/to/MU.csv"):
        load("MU", providers=("yahoo",), cache_dir=tmp_path, offline=True)
