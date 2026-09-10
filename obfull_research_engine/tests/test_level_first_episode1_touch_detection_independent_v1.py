"""Negative + unit tests for independent Episode-1 touch/detection derivation."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

from obfull_research_engine.level_first_episode1_touch_detection_independent_v1 import (  # noqa: E402
    REFERENCE_DETECTION_ISO,
    REFERENCE_WALL_FIRST_TOUCH_ISO,
    REFERENCE_ZONE_FIRST_TOUCH_ISO,
    RESEARCH_VISIT_COUNT,
    VERDICT_OK,
)
from obfull_research_engine.level_first_episode1_touch_detection_independent_v1.derive import (  # noqa: E402
    derive_all,
    derive_detection,
    derive_zone_first_touch,
)
from obfull_research_engine.level_first_episode1_touch_detection_independent_v1.inputs import (  # noqa: E402
    load_candles,
    load_trades,
    load_zone_from_mp_events,
)
from obfull_research_engine.level_first_episode1_touch_detection_independent_v1.wall_generations import (  # noqa: E402
    CoverageError,
    WallGeneration,
    build_wall_generations,
    reconstruct_or_coverage_error,
)


def test_01_zone_touch_without_iso_constant():
    zone = load_zone_from_mp_events()
    trades = load_trades()
    candles = load_candles()
    window_end = zone["zone_available_at_dt"] + timedelta(hours=2)
    touch = derive_zone_first_touch(zone=zone, trades=trades, candles=candles, window_end=window_end)
    assert touch["exchange_event_time"] == REFERENCE_ZONE_FIRST_TOUCH_ISO
    assert touch["used_first_touch_iso_as_input"] is False
    assert touch["visit_count"] == RESEARCH_VISIT_COUNT


def test_02_forbidden_config_stripped_does_not_change_result():
    a = derive_all(cfg={}, replay=None, allow_archive_replay=False)
    b = derive_all(
        cfg={"FIRST_TOUCH_ISO": "1999-01-01T00:00:00Z", "first_touch": "1999-01-01T00:00:00Z", "detection": "1999-01-01T00:00:00Z"},
        replay=None,
        allow_archive_replay=False,
    )
    # Without replay wall is missing → blocked, but zone/detection must match
    assert a["zone_first_touch"]["exchange_event_time"] == b["zone_first_touch"]["exchange_event_time"]
    assert a["detection"]["detected_at"] == b["detection"]["detected_at"]
    assert "FIRST_TOUCH_ISO" in b["stripped_forbidden_config_keys"] or "first_touch" in b["stripped_forbidden_config_keys"]


def test_03_detection_matches_reference_rule():
    zone = load_zone_from_mp_events()
    trades = load_trades()
    candles = load_candles()
    window_end = zone["zone_available_at_dt"] + timedelta(hours=2)
    touch = derive_zone_first_touch(zone=zone, trades=trades, candles=candles, window_end=window_end)
    det = derive_detection(zone=zone, zone_touch=touch, trades=trades, candles=candles, window_end=window_end)
    assert det["detected_at"] == REFERENCE_DETECTION_ISO
    assert det["used_detection_iso_as_input"] is False


def test_04_future_trades_do_not_move_earlier_touch():
    zone = load_zone_from_mp_events()
    trades = load_trades()
    candles = load_candles()
    window_end = zone["zone_available_at_dt"] + timedelta(hours=2)
    base = derive_zone_first_touch(zone=zone, trades=trades, candles=candles, window_end=window_end)
    future = list(trades) + [
        {
            "trade_id": "future-fake",
            "trade_ts": "2026-09-06T22:00:00Z",
            "price": 79775.0,
            "size": 1.0,
            "taker_side": "Buy",
            "collector_received_at": "2026-09-06T22:00:01Z",
        }
    ]
    alt = derive_zone_first_touch(zone=zone, trades=future, candles=candles, window_end=window_end)
    assert alt["exchange_event_time"] == base["exchange_event_time"]


def test_05_new_wall_generation_different_id():
    t0 = datetime(2026, 9, 6, 20, 14, 0, tzinfo=timezone.utc)
    g1 = WallGeneration("ask", 79780.0, 3, 1, t0, t0 + timedelta(seconds=10), None, None, None)
    g2 = WallGeneration("ask", 79780.0, 3, 2, t0 + timedelta(seconds=20), None, None, None, None)
    assert g1.generation_id() != g2.generation_id()
    assert g1.wall_id("ep:test") != g2.wall_id("ep:test")


def test_06_missing_checkpoint_raises_coverage():
    try:
        reconstruct_or_coverage_error(
            {"ok": True, "initial_asks": None, "initial_bids": None, "level_changes": [], "book_resets": []},
            datetime.now(timezone.utc),
        )
        assert False, "expected CoverageError"
    except CoverageError:
        pass


def test_07_generation_boundary_on_zero_cross():
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    lcs = [
        {
            "event_time": t0 + timedelta(seconds=1),
            "side": "ask",
            "price": 79780.0,
            "old_size": 0.0,
            "new_size": 5.0,
            "replay_epoch": 1,
            "apply_order": 1,
            "source_event_id": "a",
            "u": 1,
            "seq": 1,
        },
        {
            "event_time": t0 + timedelta(seconds=2),
            "side": "ask",
            "price": 79780.0,
            "old_size": 5.0,
            "new_size": 0.0,
            "replay_epoch": 1,
            "apply_order": 2,
            "source_event_id": "b",
            "u": 2,
            "seq": 2,
        },
        {
            "event_time": t0 + timedelta(seconds=3),
            "side": "ask",
            "price": 79780.0,
            "old_size": 0.0,
            "new_size": 8.0,
            "replay_epoch": 1,
            "apply_order": 3,
            "source_event_id": "c",
            "u": 3,
            "seq": 3,
        },
    ]
    gens = build_wall_generations(
        level_changes=lcs,
        book_resets=[],
        initial_asks={},
        initial_bids={},
        initial_replay_epoch=1,
        window_start=t0,
    )
    assert len(gens) == 2
    assert gens[0].generation_id() != gens[1].generation_id()
