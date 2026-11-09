"""Forming bar: collector-first with ClickHouse fallback (Research UI only)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from research_charts.collector_control import fetch_forming_candle
from research_charts.forming_from_ch import forming_bar_from_clickhouse
from research_charts.service import apply_live_forming_tip, load_trp


def _utc(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=timezone.utc)


def _make_1m(sym: str, ts: datetime, o, h, l, c, v=1.0):
    trp = load_trp()
    Candle = trp["Candle"]
    return Candle(
        timestamp=ts,
        open=float(o),
        high=float(h),
        low=float(l),
        close=float(c),
        volume=float(v),
        symbol=sym,
        timeframe="1m",
    )


def test_collector_success_uses_collector_not_ch():
    collector_bar = {
        "time": 1_700_000_000,
        "open": 1.0,
        "high": 1.1,
        "low": 0.9,
        "close": 1.05,
        "volume": 10.0,
    }
    with patch(
        "research_charts.collector_control._fetch_collector_forming",
        return_value=(collector_bar, "ok"),
    ):
        with patch("research_charts.collector_control.forming_bar_from_clickhouse") as ch:
            out = fetch_forming_candle("TUTUSDT")
    assert out is not None
    assert out["forming_source"] == "collector"
    assert out["close"] == 1.05
    ch.assert_not_called()


def test_collector_unreachable_uses_ch_fallback():
    ch_bar = {
        "time": 1_700_000_100,
        "open": 2.0,
        "high": 2.2,
        "low": 1.9,
        "close": 2.1,
        "volume": 5.0,
        "forming_source": "clickhouse_fallback",
    }
    with patch(
        "research_charts.collector_control._fetch_collector_forming",
        return_value=(None, "unreachable"),
    ):
        with patch(
            "research_charts.collector_control.forming_bar_from_clickhouse",
            return_value=ch_bar,
        ):
            out = fetch_forming_candle("TUTUSDT")
    assert out is not None
    assert out["forming_source"] == "clickhouse_fallback"


def test_collector_null_forming_uses_ch_when_no_hold():
    with patch(
        "research_charts.collector_control._fetch_collector_forming",
        return_value=(None, "null_forming"),
    ):
        with patch("research_charts.collector_control._forming_cache", {}):
            with patch(
                "research_charts.collector_control.forming_bar_from_clickhouse",
                return_value={"time": 100, "close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0, "volume": 0},
            ) as ch:
                out = fetch_forming_candle("TUTUSDT")
    assert out is not None
    ch.assert_called_once()


def test_ch_fallback_15m_partial_ohlc():
    sym = "TUTUSDT"
    t0 = _utc(2026, 10, 7, 9, 15)
    bars = [
        _make_1m(sym, _utc(2026, 10, 7, 9, 15), 1.0, 1.2, 0.9, 1.1),
        _make_1m(sym, _utc(2026, 10, 7, 9, 16), 1.1, 1.3, 1.0, 1.2),
        _make_1m(sym, _utc(2026, 10, 7, 9, 20), 1.2, 1.25, 1.15, 1.22),
    ]
    now = _utc(2026, 10, 7, 9, 21)

    def _get_1m(_sym, bucket_open):
        assert _sym == sym
        return [b for b in bars if b.timestamp >= bucket_open]

    out = forming_bar_from_clickhouse(
        sym, now=now, timeframe="15m", get_1m_candles=_get_1m
    )
    assert out is not None
    assert out["time"] == int(_utc(2026, 10, 7, 9, 20).timestamp())
    assert out["open"] == 1.0
    assert out["high"] == 1.3
    assert out["low"] == 0.9
    assert out["close"] == 1.22
    assert out["forming_source"] == "clickhouse_fallback"
    bucket_open = int(_utc(2026, 10, 7, 9, 15).timestamp())
    assert int(now.timestamp()) // 900 * 900 == bucket_open


def test_ch_fallback_empty_bucket_returns_none():
    def _empty(_sym, _start):
        return []

    out = forming_bar_from_clickhouse(
        "TUTUSDT",
        now=_utc(2026, 10, 7, 9, 21),
        get_1m_candles=_empty,
    )
    assert out is None


def test_apply_live_forming_skipped_for_historical_end():
    packed = {
        "symbol": "TUTUSDT",
        "timeframe": "15m",
        "candles": [{"time": 100, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}],
        "to": 50,
        "end": 50,
    }
    with patch(
        "research_charts.service.fetch_forming_candle",
        return_value={"time": 200, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1},
    ):
        out = apply_live_forming_tip(packed)
    assert out.get("forming") is None
    assert out["candles"][-1]["close"] == 1


def test_strict_complete_buckets_unchanged_in_load_path():
    import inspect

    from research_charts import service as svc

    src = inspect.getsource(svc._load_candles_compute)
    assert "strict_complete_buckets=True" in src


def test_host_common_decision_uses_last_closed_bar():
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[2]
        / "static/js/research/research_charts.js"
    ).read_text(encoding="utf-8")
    fn = js[js.index("function resolveScannerCommonDecisionIso") :]
    fn = fn[: fn.index("function renderEndUnixForScannerPane")]
    assert "lastClosedBarTime(pane)" in fn
    assert "for (let i = candles.length - 1" not in fn


def test_host_replay_blocks_forming_apply():
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[2]
        / "static/js/research/research_charts.js"
    ).read_text(encoding="utf-8")
    apply_fn = js[js.index("function applyFormingToPane") : js.index("let formingInflight")]
    assert "isHistoricalReplay()" in apply_fn
    poll_fn = js[js.index("async function pollForming") : js.index("async function pollIncremental")]
    assert "historyBlocksLive()" in poll_fn
