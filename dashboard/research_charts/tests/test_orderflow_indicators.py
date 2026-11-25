"""Orderflow indicators against public_trades_canonical, not bubble buckets."""

from datetime import datetime, timezone
from decimal import Decimal

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.orderflow_indicators import (
    apply_cvd,
    classify_empty,
    load_orderflow,
)

AUDIT = {
    (10, 25): (893, "485974.069", "146594.527"),
    (10, 30): (1167, "636251.203", "633307.969"),
    (10, 35): (2462, "2319051.247", "1066733.941"),
    (10, 40): (1386, "526923.235", "762363.245"),
    (10, 45): (1772, "419957.946", "2033830.164"),
    (10, 50): (1011, "361677.388", "230245.567"),
}


def _at(hour, minute):
    return datetime(2026, 11, 20, hour, minute, tzinfo=timezone.utc)


def _q3(text):
    return Decimal(text).quantize(Decimal("0.001"))


def _independent(start, end, step):
    client = clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs())
    try:
        rows = client.query(
            f"""
            SELECT
              toUnixTimestamp(toStartOfInterval(trade_ts, toIntervalSecond({int(step)}))) AS candle_open,
              count() AS prints,
              sumIf(notional, side = 'Buy') AS buy_usdt,
              sumIf(notional, side = 'Sell') AS sell_usdt
            FROM orderbook_analysis.public_trades_canonical FINAL
            PREWHERE symbol = 'SOLUSDT'
            WHERE trade_ts >= {{a:DateTime64(3, 'UTC')}}
              AND trade_ts < {{b:DateTime64(3, 'UTC')}}
            GROUP BY candle_open
            ORDER BY candle_open
            """,
            parameters={"a": start, "b": end},
            settings={"max_execution_time": 40, "max_memory_usage": 800_000_000, "max_threads": 2},
        ).result_rows
    finally:
        client.close()
    return {int(r[0]): (int(r[1]), Decimal(str(r[2])), Decimal(str(r[3]))) for r in rows}


def test_empty_candle_is_not_inferred_from_edges_alone():
    # 44s after the open is still not a completeness verdict.
    assert classify_empty(1_000, 300, 2_000, 1_044, 1_290) == "UNKNOWN"
    assert classify_empty(1_000, 300, 2_000, None, None) == "GAP"
    assert classify_empty(1_000, 300, 1_100, None, None) == "FORMING"


def test_cvd_breaks_on_gap_and_restarts():
    bars = [
        {"time": 0, "status": "MEASURED", "delta_usdt": "10"},
        {"time": 300, "status": "GAP", "delta_usdt": None},
        {"time": 600, "status": "MEASURED", "delta_usdt": "4"},
        {"time": 900, "status": "UNKNOWN", "delta_usdt": None},
        {"time": 1200, "status": "MEASURED", "delta_usdt": "1"},
    ]
    apply_cvd(bars, reset="range_start")
    assert bars[0]["cvd_usdt"] == "10.00000000"
    assert bars[0]["segment_start"] is True
    assert bars[1]["cvd_usdt"] is None
    assert bars[2]["segment_start"] is True
    assert bars[2]["cvd_usdt"] == "4.00000000"
    assert bars[2]["segment_id"] != bars[0]["segment_id"]
    assert bars[4]["segment_start"] is True
    assert bars[4]["cvd_usdt"] == "1.00000000"


def test_sol_5m_matches_audit_and_independent_sql():
    start, end = _at(10, 25), _at(10, 55)
    payload = load_orderflow(
        "SOLUSDT", "5m", start, end, as_of=_at(10, 55), cvd_reset="range_start"
    )
    ref = _independent(start, end, 300)
    assert payload["baseline"]["available"] is False
    assert payload["coverage_proof"]["collector_sequence_persisted"] is False
    by_time = {b["time"]: b for b in payload["bars"]}
    assert set(by_time) == set(ref)
    for (hour, minute), (prints, buy, sell) in AUDIT.items():
        bar = by_time[int(_at(hour, minute).timestamp())]
        assert bar["status"] == "MEASURED"
        assert bar["coverage"] == "UNPROVEN"
        assert bar["prints"] == prints
        assert _q3(bar["buy_usdt"]) == Decimal(buy)
        assert _q3(bar["sell_usdt"]) == Decimal(sell)
        assert bar["highlight"] is False
        assert Decimal(bar["avg_usdt_per_print"]) > 0
        exact = ref[bar["time"]]
        assert bar["prints"] == exact[0]
        assert Decimal(bar["buy_usdt"]) == exact[1]
        assert Decimal(bar["sell_usdt"]) == exact[2]
        assert Decimal(bar["delta_usdt"]) == exact[1] - exact[2]


def test_higher_timeframes_sum_the_same_prints():
    start, end = _at(10, 0), _at(11, 0)
    as_of = _at(11, 0)
    five = load_orderflow("SOLUSDT", "5m", start, end, as_of=as_of, cvd_reset="range_start")
    ref15 = _independent(start, end, 900)
    ref30 = _independent(start, end, 1800)
    ref60 = _independent(start, end, 3600)
    for tf, ref in (("15m", ref15), ("30m", ref30), ("1h", ref60)):
        payload = load_orderflow("SOLUSDT", tf, start, end, as_of=as_of, cvd_reset="range_start")
        got = {b["time"]: b for b in payload["bars"] if b["status"] == "MEASURED"}
        assert set(got) == set(ref)
        for t, bar in got.items():
            assert bar["prints"] == ref[t][0]
            assert Decimal(bar["buy_usdt"]) == ref[t][1]
            assert Decimal(bar["sell_usdt"]) == ref[t][2]
    # 10:30 15m equals the three 5m candles inside it.
    step = {b["time"]: b for b in five["bars"]}
    open_15 = int(_at(10, 30).timestamp())
    parts = [step[open_15 + i * 300] for i in range(3)]
    merged = load_orderflow("SOLUSDT", "15m", _at(10, 30), _at(10, 45), as_of=_at(10, 45), cvd_reset="range_start")
    bar = merged["bars"][0]
    assert bar["prints"] == sum(p["prints"] for p in parts)
    assert Decimal(bar["buy_usdt"]) == sum(Decimal(p["buy_usdt"]) for p in parts)
    assert Decimal(bar["sell_usdt"]) == sum(Decimal(p["sell_usdt"]) for p in parts)


def test_4h_uses_the_same_table():
    # Closed 4h bucket. The 08:00 candle was still forming while this test ran.
    start, end = _at(4, 0), _at(8, 0)
    payload = load_orderflow("SOLUSDT", "4h", start, end, as_of=end, cvd_reset="range_start")
    ref = _independent(start, end, 14400)
    bar = next(b for b in payload["bars"] if b["time"] == int(start.timestamp()))
    assert bar["status"] == "MEASURED"
    assert bar["prints"] == ref[bar["time"]][0]
    assert Decimal(bar["buy_usdt"]) == ref[bar["time"]][1]
    assert Decimal(bar["sell_usdt"]) == ref[bar["time"]][2]


def test_confirmed_hole_has_no_zero_delta():
    start = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)
    payload = load_orderflow("SOLUSDT", "5m", start, end, as_of=end, cvd_reset="range_start")
    assert payload["bars"]
    for bar in payload["bars"]:
        assert bar["status"] == "GAP"
        assert bar["delta_usdt"] is None
        assert bar["prints"] is None
        assert bar["cvd_usdt"] is None
        assert bar["buy_usdt"] is None
