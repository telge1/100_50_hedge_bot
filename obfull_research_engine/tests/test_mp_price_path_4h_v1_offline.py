"""Offline tests for mp_price_path_4h_v1."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from obfull_research_engine.mp_price_path_4h_v1.candles import Candle1m
from obfull_research_engine.mp_price_path_4h_v1.geometry import (
    adverse_pct,
    favorable_pct,
    mae_from_extremes,
    mfe_from_extremes,
    signed_return_pct,
)
from obfull_research_engine.mp_price_path_4h_v1.params import NS, PILOT_MAX_EVENTS
from obfull_research_engine.mp_price_path_4h_v1.path_engine import (
    analyze_event_path,
    first_hit,
    select_path_candles,
)
from obfull_research_engine.mp_price_path_4h_v1.report import write_report
from obfull_research_engine.mp_price_path_4h_v1.selection import build_non_overlapping_4h
from obfull_research_engine.mp_price_path_4h_v1.wall_filter import (
    event_passes_wall_persistence,
)


def _candles_from_ohlc(start_ns: int, bars: list[tuple[float, float, float, float]]) -> list[Candle1m]:
    # align to minute boundary
    start_ns = (int(start_ns) // (60 * NS)) * (60 * NS)
    out = []
    for i, (o, h, l, c) in enumerate(bars):
        out.append(
            Candle1m(
                open_time_ns=start_ns + i * 60 * NS,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=1.0,
            )
        )
    return out, start_ns


def test_long_short_mfe_mae_and_signed_returns_percent():
    # LONG: price 100 -> high 101 = +1%, low 99 = 1% mae
    assert favorable_pct(trade_side="LONG", trigger_price=100, future_price=101) == pytest.approx(1.0)
    assert mfe_from_extremes(trade_side="LONG", trigger_price=100, high=101, low=99) == pytest.approx(1.0)
    assert mae_from_extremes(trade_side="LONG", trigger_price=100, high=101, low=99) == pytest.approx(1.0)
    assert signed_return_pct(trade_side="LONG", trigger_price=100, future_close=102) == pytest.approx(2.0)
    # SHORT
    assert favorable_pct(trade_side="SHORT", trigger_price=100, future_price=99) == pytest.approx(1.0)
    assert mfe_from_extremes(trade_side="SHORT", trigger_price=100, high=101, low=99) == pytest.approx(1.0)
    assert mae_from_extremes(trade_side="SHORT", trigger_price=100, high=101, low=99) == pytest.approx(1.0)
    assert signed_return_pct(trade_side="SHORT", trigger_price=100, future_close=98) == pytest.approx(2.0)
    assert adverse_pct(trade_side="LONG", trigger_price=100, future_price=99) == pytest.approx(1.0)


def test_mae_before_mfe_exclusive_inclusive_and_ambiguous():
    start0 = 1_700_000_000 * NS
    bars = [
        (100, 100.0, 99.5, 99.7),  # mae 0.5%
        (99.7, 100.3, 99.4, 100.2),  # mfe 0.3%, mae 0.6% same bar
    ]
    candles, start = _candles_from_ohlc(start0, bars + [(100.2, 100.2, 100.2, 100.2)] * 238)
    res = analyze_event_path(
        candles,
        event_id="e1",
        trigger_ts_ns=start,
        trigger_price=100.0,
        trade_side="LONG",
    )
    h = next(x for x in res["horizons"] if x["horizon_min"] == 240)
    assert h["mae_before_mfe_peak_exclusive_pct"] == pytest.approx(0.5)
    assert h["mae_before_mfe_peak_inclusive_pct"] == pytest.approx(0.6)
    assert h["intrabar_order_ambiguous"] is True


def test_first_hit_target_stop_neither_ambiguous_censored():
    start0 = 1_800_000_000 * NS
    bars = [(100, 100.2, 100.0, 100.15), (100.15, 100.15, 99.5, 99.6)]
    candles, start = _candles_from_ohlc(start0, bars + [(99.6, 99.6, 99.6, 99.6)] * 238)
    res = analyze_event_path(
        candles, event_id="e2", trigger_ts_ns=start, trigger_price=100.0, trade_side="LONG"
    )
    hits = res["first_hits"]
    tf = next(h for h in hits if h["horizon_min"] == 30 and abs(h["target_pct"] - 0.10) < 1e-12 and abs(h["stop_pct"] - 0.10) < 1e-12)
    assert tf["result"] == "TARGET_FIRST"

    bars2 = [(100, 100.0, 99.8, 99.85), (99.85, 100.3, 99.85, 100.2)]
    candles2, start = _candles_from_ohlc(start0, bars2 + [(100.2, 100.2, 100.2, 100.2)] * 238)
    res2 = analyze_event_path(
        candles2, event_id="e3", trigger_ts_ns=start, trigger_price=100.0, trade_side="LONG"
    )
    sf = next(h for h in res2["first_hits"] if h["horizon_min"] == 30 and abs(h["target_pct"]-0.10)<1e-12 and abs(h["stop_pct"]-0.10)<1e-12)
    assert sf["result"] == "STOP_FIRST"

    bars3 = [(100, 100.01, 99.99, 100.0)] * 240
    candles3, start = _candles_from_ohlc(start0, bars3)
    res3 = analyze_event_path(
        candles3, event_id="e4", trigger_ts_ns=start, trigger_price=100.0, trade_side="LONG"
    )
    nf = next(h for h in res3["first_hits"] if h["horizon_min"] == 30 and abs(h["target_pct"]-0.50)<1e-12 and abs(h["stop_pct"]-0.50)<1e-12)
    assert nf["result"] == "NEITHER"

    bars4 = [(100, 100.2, 99.8, 100.0)] + [(100, 100, 100, 100)] * 239
    candles4, start = _candles_from_ohlc(start0, bars4)
    res4 = analyze_event_path(
        candles4, event_id="e5", trigger_ts_ns=start, trigger_price=100.0, trade_side="LONG"
    )
    af = next(h for h in res4["first_hits"] if h["horizon_min"] == 30 and abs(h["target_pct"]-0.10)<1e-12 and abs(h["stop_pct"]-0.10)<1e-12)
    assert af["result"] == "AMBIGUOUS"

    short, start = _candles_from_ohlc(start0, [(100, 100.1, 99.9, 100.0)] * 10)
    st = select_path_candles(short, trigger_ts_ns=start, horizon_min=30)
    assert st is None
    hit = first_hit(
        [{"bar_mfe_pct": 0, "bar_mae_pct": 0, "running_mfe_pct": 0, "running_mae_pct": 0}] * 5,
        target_pct=0.1,
        stop_pct=0.1,
        horizon_min=30,
    )
    assert hit["result"] == "CENSORED"


def test_horizons_and_no_future_before_trigger():
    start0 = 1_900_000_000 * NS
    bars = [(100 + i * 0.01, 100 + i * 0.01 + 0.05, 100 + i * 0.01 - 0.02, 100 + i * 0.01) for i in range(240)]
    candles, start = _candles_from_ohlc(start0, bars)
    trig = start + 30 * NS
    res = analyze_event_path(
        candles, event_id="e6", trigger_ts_ns=trig, trigger_price=100.0, trade_side="LONG"
    )
    assert res["ok"]
    assert res["entry_candle_partial"] is True
    assert res["path_rows"][0]["candle_ts_ns"] == start
    for h in (5, 30, 60, 120, 240):
        row = next(x for x in res["horizons"] if x["horizon_min"] == h)
        assert row["is_complete"] is True
        assert row["candle_count"] == h


def test_target_010_and_underwater_recovery():
    start0 = 2_000_000_000 * NS
    bars = [(100, 100.0, 99.7, 99.8)] * 3 + [(99.8, 100.15, 99.8, 100.12)] + [(100.12, 100.12, 100.12, 100.12)] * 236
    candles, start = _candles_from_ohlc(start0, bars)
    res = analyze_event_path(
        candles, event_id="e7", trigger_ts_ns=start, trigger_price=100.0, trade_side="LONG"
    )
    t = next(x for x in res["targets"] if abs(x["target_pct"] - 0.10) < 1e-12)
    assert t["target_reached"] is True
    assert t["mae_before_target_exclusive_pct"] == pytest.approx(0.3)
    uw = res["underwater"]
    assert uw["minutes_until_first_positive"] is not None
    assert uw["total_underwater_minutes"] >= 1


def test_non_overlapping_4h_causal():
    events = [
        {"event_id": "a", "trigger_ts_ns": 0, "trade_side": "LONG", "trigger_price": 1},
        {"event_id": "b", "trigger_ts_ns": 1 * 3600 * NS, "trade_side": "LONG", "trigger_price": 1},
        {"event_id": "c", "trigger_ts_ns": 5 * 3600 * NS, "trade_side": "SHORT", "trigger_price": 1},
    ]
    rows = build_non_overlapping_4h(events)
    by = {r["event_id"]: r["selected_non_overlapping_4h"] for r in rows}
    assert by["a"] is True
    assert by["b"] is False
    assert by["c"] is True


def test_wall_persistence_unchanged_thresholds():
    frozen = {
        "thresholds": {"wall_present_baseline_fraction": 0.5, "wall_survival_after_hits": 0.1},
        "directions": {"wall_present_baseline_fraction": 1, "wall_survival_after_hits": -1},
    }
    assert event_passes_wall_persistence(
        {"wall_present_baseline_fraction": 0.6, "wall_survival_after_hits": 0.05}, frozen
    )
    assert not event_passes_wall_persistence(
        {"wall_present_baseline_fraction": 0.4, "wall_survival_after_hits": 0.05}, frozen
    )


def test_report_percent_no_bps_columns(tmp_path: Path):
    write_report(tmp_path, verdict="PRICE_PATH_4H_SUCCESS", summary={"median_mfe_pct": 0.2})
    text = (tmp_path / "PRICE_PATH_4H_REPORT.md").read_text(encoding="utf-8")
    assert "percent" in text.lower()
    assert text.lower().count("bps") <= 2


def test_pilot_cap_and_deterministic():
    start0 = 2_100_000_000 * NS
    bars = [(100, 100.1, 99.9, 100.0)] * 240
    candles, start = _candles_from_ohlc(start0, bars)
    a = analyze_event_path(
        candles, event_id="det", trigger_ts_ns=start, trigger_price=100.0, trade_side="LONG"
    )
    b = analyze_event_path(
        candles, event_id="det", trigger_ts_ns=start, trigger_price=100.0, trade_side="LONG"
    )
    assert a["horizons"] == b["horizons"]
    assert PILOT_MAX_EVENTS == 20
    src = inspect.getsource(
        __import__(
            "obfull_research_engine.mp_price_path_4h_v1.run_cli", fromlist=["run_price_path"]
        ).run_price_path
    )
    assert "run_offline_pipeline_v2" not in src
