"""Causal LLD landscape timeline adapter tests."""

from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

_repo = Path(__file__).resolve().parents[4]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
_dash = _repo / "backtester" / "short"
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from pool_pattern.market import ensure_paths

ensure_paths()

from dashboard.research_charts.chart_causal_lld_timeline_v1 import (
    CAUSAL_LLD_LANDSCAPE_NAMESPACE,
    chart_causal_lld_timeline,
    clear_causal_lld_cache,
    segments_hash,
    ui_selected_ids_at_index,
    visible_pool_ids_at,
)
from dashboard.research_charts.chart_scanner_pools_v1 import chart_scanner_pool_snapshot
from dashboard.research_charts.frozen_lld_config_v1 import HISTORY_WEEKS, PANE_FROM_UTC
from dashboard.research_charts.lld_research_kernel import (
    candle_stamps,
    load_pane_candles,
    scanner_pools_for_index,
)
from dashboard.research_charts.short_pool_oracle_v1 import parse_decision_iso
from pool_scan.clock import bar_close

_TRP = Path("/home/telgenbuescher/projects/trading_research_platform").is_dir()

TUT = "TUTUSDT"
FROM_ISO = "2026-09-24T00:00:00+00:00"
TO_ISO = "2026-10-01T12:00:00+00:00"
PARITY_ISOS = (
    "2026-09-28T12:00:00+00:00",
    "2026-09-29T12:00:00+00:00",
    "2026-09-30T12:00:00+00:00",
)


@pytest.fixture(scope="module")
def tut_payload():
    clear_causal_lld_cache()
    return chart_causal_lld_timeline(TUT, FROM_ISO, TO_ISO, use_response_cache=False)


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_known_not_after_start(tut_payload):
    for z in tut_payload["zones"]:
        known = z.get("known_at")
        if not known:
            continue
        assert int(parse_decision_iso(known).timestamp()) <= int(z["start_timestamp"])


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_causal_superset_of_common(tut_payload):
    _, candles = load_pane_candles(
        TUT,
        "15m",
        from_unix=int(PANE_FROM_UTC.timestamp()),
        to_unix=int(parse_decision_iso(TO_ISO).timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    stamps = candle_stamps(candles)
    cfg = None
    from dashboard.research_charts.frozen_lld_config_v1 import frozen_lld_config_for_timeframe_v1

    cfg = frozen_lld_config_for_timeframe_v1("15m")
    cache: dict = {}
    zones = tut_payload["zones"]
    for iso in PARITY_ISOS:
        idx = next(i for i, o in enumerate(stamps) if bar_close(o, "15m") == parse_decision_iso(iso))
        dc = bar_close(stamps[idx], "15m")
        avail, ui, _ = scanner_pools_for_index(candles, "15m", idx, cfg, cache=cache)
        causal_ids = visible_pool_ids_at(zones, int(dc.timestamp()))
        common_ids = {r["pool_id"] for r in avail}
        assert common_ids <= causal_ids


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_prefix_parity_ui_selected():
    _, candles = load_pane_candles(
        TUT,
        "15m",
        from_unix=int(PANE_FROM_UTC.timestamp()),
        to_unix=int(parse_decision_iso(TO_ISO).timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    stamps = candle_stamps(candles)
    from dashboard.research_charts.frozen_lld_config_v1 import frozen_lld_config_for_timeframe_v1

    cfg = frozen_lld_config_for_timeframe_v1("15m")
    for iso in PARITY_ISOS:
        idx = next(i for i, o in enumerate(stamps) if bar_close(o, "15m") == parse_decision_iso(iso))
        dc = bar_close(stamps[idx], "15m")
        full = ui_selected_ids_at_index(candles, "15m", idx, dc, cfg, cache={})
        prefix = candles[: idx + 1]
        pre = ui_selected_ids_at_index(prefix, "15m", idx, dc, cfg, cache={})
        assert full == pre


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_density_regression_order_of_magnitude(tut_payload):
    assert tut_payload["pool_count"] >= 2000
    assert tut_payload["segment_count"] >= 4000


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_no_zones_beyond_to_close(tut_payload):
    to_unix = int(parse_decision_iso(TO_ISO).timestamp())
    for z in tut_payload["zones"]:
        assert int(z["start_timestamp"]) <= to_unix
        reason = z.get("end_reason")
        if reason != "window_end_active":
            assert int(z["end_timestamp"]) <= to_unix


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_cache_hash_stable(tut_payload):
    clear_causal_lld_cache()
    a = chart_causal_lld_timeline(TUT, FROM_ISO, TO_ISO, use_response_cache=True)
    b = chart_causal_lld_timeline(TUT, FROM_ISO, TO_ISO, use_response_cache=True)
    assert a["segments_hash"] == b["segments_hash"]


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_break_end_before_deselection_when_earlier(tut_payload):
    zones = tut_payload["zones"]
    broken = [z for z in zones if z.get("end_reason") == "broken"]
    assert broken, "expected some broken segments in window"
    for z in broken[:20]:
        ba = z.get("break_at")
        if not ba:
            continue
        assert int(z["end_timestamp"]) <= int(parse_decision_iso(ba).timestamp()) + 1


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_bounds_change_splits_segments(tut_payload):
    by_pool: dict[str, list[dict]] = {}
    for z in tut_payload["zones"]:
        by_pool.setdefault(z["pool_id"], []).append(z)
    multi = [pid for pid, zs in by_pool.items() if len(zs) > 1]
    if not multi:
        pytest.skip("no multi-segment pools in window")
    for pid in multi[:5]:
        zs = sorted(by_pool[pid], key=lambda z: int(z["segment_index"]))
        for a, b in zip(zs, zs[1:]):
            assert int(a["end_timestamp"]) <= int(b["start_timestamp"]) + 900 * 4


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_common_snapshot_unchanged():
    snap = chart_scanner_pool_snapshot(TUT, PARITY_ISOS[0])
    assert snap["namespace"] != CAUSAL_LLD_LANDSCAPE_NAMESPACE
    assert "overlays" in snap


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("days", (1, 3, 7))
def test_runtime_benchmark(days: int):
    end = parse_decision_iso("2026-10-01T12:00:00+00:00")
    start = end - timedelta(days=days - 1)
    start = start.replace(hour=0, minute=15)
    clear_causal_lld_cache()
    t0 = time.perf_counter()
    payload = chart_causal_lld_timeline(
        TUT, start.isoformat(), end.isoformat(), use_response_cache=False
    )
    elapsed = time.perf_counter() - t0
    print(
        f"RUNTIME_{days}D={elapsed:.2f}s seg={payload['segment_count']} pools={payload['pool_count']}"
    )
    if days == 7:
        assert elapsed < 30.0
