"""Historical COMMON timeline adapter: lifecycle, parity, band regression."""

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

from bot.common_causal_pools_v1 import common_causal_pool_universe
from dashboard.research_charts.chart_scanner_pool_timeline_v1 import (
    active_pool_ids_at,
    chart_scanner_pool_timeline,
    clear_timeline_cache,
    segments_hash,
    timeline_parity_at,
)
from dashboard.research_charts.chart_scanner_pools_v1 import chart_scanner_pool_snapshot
from dashboard.research_charts.short_pool_oracle_v1 import parse_decision_iso

_TRP = Path("/home/telgenbuescher/projects/trading_research_platform").is_dir()

TUT = "TUTUSDT"
FROM_ISO = "2026-09-22T00:15:00+00:00"
TO_ISO = "2026-09-24T23:45:00+00:00"
BAND_LOW = 0.0225
BAND_HIGH = 0.0270
EXPECTED_HISTORICAL_BAND_POOL_COUNT = 87

PARITY_DECISIONS = (
    "2026-09-22T12:00:00+00:00",
    "2026-09-23T12:00:00+00:00",
    "2026-09-24T12:00:00+00:00",
    "2026-09-24T21:00:00+00:00",
)


def _bounds_at(zones: list[dict], decision_unix: int) -> dict[str, tuple[float, float]]:
    active = active_pool_ids_at(zones, decision_unix)
    out: dict[str, tuple[float, float]] = {}
    for z in zones:
        pid = str(z["pool_id"])
        if pid not in active or pid in out:
            continue
        if pid not in active_pool_ids_at([z], decision_unix):
            continue
        lo = float(z.get("bottom_price") or z["lower_price"])
        hi = float(z.get("top_price") or z["upper_price"])
        out[pid] = (lo, hi)
    return out


def _snapshot_bounds(symbol: str, decision_iso: str) -> dict[str, tuple[float, float]]:
    snap = common_causal_pool_universe(symbol, parse_decision_iso(decision_iso))
    return {
        str(r["pool_id"]): (float(r["bottom"]), float(r["top"])) for r in snap.available
    }


@pytest.fixture(scope="module")
def tut_timeline():
    clear_timeline_cache()
    return chart_scanner_pool_timeline(TUT, FROM_ISO, TO_ISO, use_response_cache=True)


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_known_not_after_start(tut_timeline):
    for z in tut_timeline["zones"]:
        known = z.get("known_at")
        if not known:
            continue
        known_ts = int(parse_decision_iso(known).timestamp())
        assert known_ts <= int(z["start_timestamp"])


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_timeline_vs_snapshot_parity(tut_timeline):
    zones = tut_timeline["zones"]
    for decision_iso in PARITY_DECISIONS:
        parity = timeline_parity_at(tut_timeline, TUT, decision_iso)
        assert parity["ids_match"] is True
        dc_unix = int(parse_decision_iso(decision_iso).timestamp())
        tb = _bounds_at(zones, dc_unix)
        sb = _snapshot_bounds(TUT, decision_iso)
        assert set(tb) == set(sb)
        for pid, bounds in sb.items():
            assert tb[pid] == bounds


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_tut_band_87_regression(tut_timeline):
    ids: set[str] = set()
    for z in tut_timeline["zones"]:
        lo = float(z.get("bottom_price") or z["lower_price"])
        hi = float(z.get("top_price") or z["upper_price"])
        if hi < BAND_LOW or lo > BAND_HIGH:
            continue
        ids.add(str(z["pool_id"]))
    assert len(ids) == EXPECTED_HISTORICAL_BAND_POOL_COUNT


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_window_end_active_segments(tut_timeline):
    to_unix = int(parse_decision_iso(TO_ISO).timestamp())
    active = active_pool_ids_at(tut_timeline["zones"], to_unix)
    snap = common_causal_pool_universe(TUT, parse_decision_iso(TO_ISO))
    assert active == set(snap.available_pool_ids)


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_cache_identical_hash(tut_timeline):
    clear_timeline_cache()
    a = chart_scanner_pool_timeline(TUT, FROM_ISO, TO_ISO, use_response_cache=True)
    b = chart_scanner_pool_timeline(TUT, FROM_ISO, TO_ISO, use_response_cache=True)
    assert a["segments_hash"] == b["segments_hash"]
    assert a["segment_count"] == b["segment_count"]


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_multi_segment_if_reentry(tut_timeline):
    by_pool: dict[str, list[int]] = {}
    for z in tut_timeline["zones"]:
        pid = str(z["pool_id"])
        by_pool.setdefault(pid, []).append(int(z["segment_index"]))
    multi = [pid for pid, idxs in by_pool.items() if max(idxs) > 0]
    if multi:
        for pid in multi:
            segs = [z for z in tut_timeline["zones"] if z["pool_id"] == pid]
            segs.sort(key=lambda s: int(s["segment_index"]))
            for i in range(len(segs) - 1):
                assert int(segs[i]["end_timestamp"]) <= int(segs[i + 1]["start_timestamp"])


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_snapshot_route_unchanged():
    snap = chart_scanner_pool_snapshot(TUT, PARITY_DECISIONS[0])
    assert snap["pool_count"] >= 0
    assert "overlays" in snap


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_no_zones_beyond_to_close(tut_timeline):
    to_unix = int(parse_decision_iso(TO_ISO).timestamp())
    for z in tut_timeline["zones"]:
        assert int(z["start_timestamp"]) <= to_unix
        reason = z.get("end_reason")
        if reason != "window_end_active":
            assert int(z["end_timestamp"]) <= to_unix


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("days", (1, 3, 7))
def test_timeline_runtime_benchmark(days: int):
    """Report wall time; 7d should stay within ~30s uncached."""
    end = parse_decision_iso("2026-09-24T23:45:00+00:00")
    start = end.replace(hour=0, minute=15) - timedelta(days=days - 1)
    from_iso = start.isoformat()
    clear_timeline_cache()
    t0 = time.perf_counter()
    payload = chart_scanner_pool_timeline(
        TUT, from_iso, end.isoformat(), use_response_cache=False
    )
    elapsed = time.perf_counter() - t0
    size = len(json.dumps(payload, separators=(",", ":")))
    print(
        f"RUNTIME_{days}D={elapsed:.2f}s "
        f"segments={payload['segment_count']} "
        f"pools={payload['pool_count']} "
        f"bytes={size}"
    )
    if days == 7:
        assert elapsed < 30.0, f"7d timeline too slow: {elapsed:.1f}s"
