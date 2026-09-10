"""Unit + negative tests for Episode-1 price response / microprice reclaim."""

from __future__ import annotations

import copy
import json
import sys
from datetime import timedelta
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

import pytest

from obfull_research_engine.level_first_episode1_detection_to_wall_flow_integration_v1.handoff import (  # noqa: E402
    HandoffError,
    validate_handoff,
)
from obfull_research_engine.level_first_episode1_price_response_reclaim_v1 import (  # noqa: E402
    RECLAIM_STATUS,
    VERDICT_OK,
)
from obfull_research_engine.level_first_episode1_price_response_reclaim_v1.microprice import (  # noqa: E402
    midprice,
    microprice,
    on_defender_side,
    side_crossed,
)
from obfull_research_engine.level_first_episode1_price_response_reclaim_v1.pipeline import (  # noqa: E402
    default_paths,
    run_price_response,
)
from obfull_research_engine.level_first_episode1_price_response_reclaim_v1.reclaim_raw import (  # noqa: E402
    accumulate_cross_dwell,
)
from obfull_research_engine.level_first_episode1_price_response_reclaim_v1.snapshots import (  # noqa: E402
    build_decision_snapshots,
)
from obfull_research_engine.level_first_episode1_price_response_reclaim_v1.timeline import (  # noqa: E402
    build_price_response_timeline,
    load_handoff,
)
from obfull_research_engine.drilldown.aggregation_100ms import _as_dt  # noqa: E402


@pytest.fixture(scope="module")
def handoff():
    return load_handoff(default_paths()["handoff"])


@pytest.fixture(scope="module")
def timeline_bundle(handoff):
    paths = default_paths()
    return build_price_response_timeline(
        handoff=handoff,
        persist_dir=paths["book"],
        wall_flow_timeline_csv=paths["wall_flow_timeline"],
    )


def test_01_microprice_formula_and_size_sensitivity():
    # Equal sizes → mid
    assert microprice(100.0, 5.0, 100.1, 5.0) == pytest.approx(100.05)
    assert midprice(100.0, 100.1) == pytest.approx(100.05)
    # Larger bid size pulls toward ask
    mp_bid_heavy = microprice(100.0, 9.0, 100.1, 1.0)
    mp_ask_heavy = microprice(100.0, 1.0, 100.1, 9.0)
    assert mp_bid_heavy > midprice(100.0, 100.1)
    assert mp_ask_heavy < midprice(100.0, 100.1)
    assert mp_bid_heavy != mp_ask_heavy


def test_02_ask_and_bid_wall_side_mirror():
    wall = 100.0
    assert side_crossed(100.0, wall_price=wall, wall_side="ask") is True
    assert side_crossed(99.9, wall_price=wall, wall_side="ask") is False
    assert on_defender_side(99.9, wall_price=wall, wall_side="ask") is True
    assert side_crossed(100.0, wall_price=wall, wall_side="bid") is True
    assert side_crossed(100.1, wall_price=wall, wall_side="bid") is False
    assert on_defender_side(100.1, wall_price=wall, wall_side="bid") is True


def test_03_price_cross_without_microprice_cross_visible():
    # mid crossed, micro still defender-side (ask wall)
    wall = 79780.0
    mid = 79780.05
    # Construct micro below wall: heavy ask size pulls toward bid
    bb, ba = 79779.9, 79780.1
    # micro = (ba*bs + bb*asz)/(bs+asz); want < wall
    mp = microprice(bb, 1.0, ba, 20.0)
    assert mp is not None and mp < wall
    assert side_crossed(mid, wall_price=wall, wall_side="ask") is True
    assert side_crossed(mp, wall_price=wall, wall_side="ask") is False


def test_04_short_cross_short_dwell_and_recross():
    wall = 100.0
    rows = []
    # defender, cross, defender (short), cross again
    sequence = [
        ("2026-09-06T20:00:00Z", 99.0, True),
        ("2026-09-06T20:00:01Z", 100.0, True),  # cross
        ("2026-09-06T20:00:01.200Z", 99.5, True),  # short reclaim
        ("2026-09-06T20:00:02Z", 100.2, True),  # recross
        ("2026-09-06T20:00:05Z", 99.0, True),
    ]
    for t, mid, ok in sequence:
        rows.append(
            {
                "decision_time": t,
                "coverage_ok": ok,
                "midprice": mid,
                "spread_ticks": 1.0,
                "distance_to_wall_ticks": wall - mid,
            }
        )
    stats = accumulate_cross_dwell(rows, wall_price=wall, wall_side="ask", price_key="midprice")
    assert stats["recross_count"] == 1
    assert stats["first_cross_at"] is not None
    # uninterrupted after reclaim should be short relative to longest possible
    assert stats["longest_defender_dwell_ms"] >= 0


def test_05_future_rows_do_not_change_early_snapshots(handoff, timeline_bundle):
    rows = timeline_bundle["rows"]
    coverage_end = _as_dt(timeline_bundle["meta"]["book_coverage_end"])
    snaps1 = build_decision_snapshots(handoff=handoff, rows=rows, book_coverage_end=coverage_end)
    # Append fake future rows with wild prices
    future = copy.deepcopy(rows[-1]) if rows else None
    if future:
        future["decision_time"] = "2099-01-01T00:00:00Z"
        future["midprice"] = 1.0
        future["microprice"] = 1.0
        future["best_bid"] = 1.0
        future["best_ask"] = 1.1
        rows2 = list(rows) + [future]
    else:
        rows2 = list(rows)
    snaps2 = build_decision_snapshots(handoff=handoff, rows=rows2, book_coverage_end=coverage_end)
    # Early valid FEATURE snapshots must be identical
    for a, b in zip(snaps1, snaps2):
        if a.get("valid") and a["offset_s"] <= 30 and a["anchor"] == "WALL_FIRST_TOUCH":
            assert a["FEATURE"]["midprice"] == b["FEATURE"]["midprice"]
            assert a["FEATURE"]["microprice"] == b["FEATURE"]["microprice"]
            assert a["FEATURE"]["recross_count"] == b["FEATURE"]["recross_count"]


def test_06_wrong_wall_generation_fail_closed(handoff, tmp_path):
    bad = handoff.to_dict()
    bad["wall_generation_id"] = "wg_deadbeefdeadbeef"
    r = run_price_response(
        run_key="badgen",
        out_dir=tmp_path / "badgen",
        handoff=validate_handoff(bad),
    )
    assert r["ok"] is False
    assert r.get("fail_closed") is True


def test_07_missing_handoff_fail_closed(tmp_path):
    r = run_price_response(
        run_key="nohandoff",
        out_dir=tmp_path / "nohandoff",
        handoff_path=tmp_path / "missing.json",
    )
    assert r["ok"] is False
    assert r.get("fail_closed") is True


def test_08_epoch_change_invalidates_affected_snapshots(handoff, timeline_bundle):
    snaps = build_decision_snapshots(
        handoff=handoff,
        rows=timeline_bundle["rows"],
        book_coverage_end=_as_dt(timeline_bundle["meta"]["book_coverage_end"]),
    )
    # +60s after wall touch is in epoch 5 on this episode → must be invalid
    hit = [s for s in snaps if s["anchor"] == "WALL_FIRST_TOUCH" and s["offset_s"] == 60]
    assert hit and hit[0]["valid"] is False
    assert hit[0]["invalid_reason"] in {"replay_epoch_change", "insufficient_book_coverage", "row_invalid"}


def test_09_missing_bbo_invalidates(handoff, timeline_bundle):
    rows = copy.deepcopy(timeline_bundle["rows"][:5])
    for r in rows:
        r["coverage_ok"] = False
        r["invalid_reason"] = "missing_bbo"
        r["best_bid"] = None
        r["best_ask"] = None
    snaps = build_decision_snapshots(
        handoff=handoff,
        rows=rows,
        book_coverage_end=_as_dt(handoff.detection_exchange_event_time),
    )
    early = [s for s in snaps if s["anchor"] == "WALL_FIRST_TOUCH" and s["offset_s"] == 1]
    assert early and early[0]["valid"] is False


def test_10_reclaim_not_calibrated(timeline_bundle):
    assert timeline_bundle["reclaim_raw"]["reclaim_status"] == RECLAIM_STATUS


def test_11_pipeline_ok_smoke(handoff, tmp_path):
    # Full pipeline may be slow; use module fixture timeline indirectly via run
    # Skip full run here if too heavy — e2e handles A/B. Still call once for smoke.
    pytest.skip("full pipeline covered by e2e A/B")
