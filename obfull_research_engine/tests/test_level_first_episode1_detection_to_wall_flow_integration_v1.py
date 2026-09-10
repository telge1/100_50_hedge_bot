"""Integration negative + contract tests."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

import pytest

from obfull_research_engine.level_first_episode1_detection_to_wall_flow_integration_v1.adapter import (  # noqa: E402
    run_detector_stage,
    run_integrated,
    run_wall_flow_from_handoff,
    book_persist_dir,
)
from obfull_research_engine.level_first_episode1_detection_to_wall_flow_integration_v1.handoff import (  # noqa: E402
    HandoffError,
    handoff_from_derivation,
    validate_handoff,
    wall_flow_inputs_from_handoff,
)
from obfull_research_engine.level_first_episode1_detection_to_wall_flow_integration_v1 import (  # noqa: E402
    FROZEN_WALL_FLOW_SEMANTIC_HASH,
    VERDICT_OK,
)


@pytest.fixture(scope="module")
def derivation_and_handoff():
    der = run_detector_stage()
    h = handoff_from_derivation(der)
    return der, h


def test_01_handoff_has_required_timing_and_generation(derivation_and_handoff):
    _, h = derivation_and_handoff
    d = h.to_dict()
    assert d["zone_first_touch_exchange_event_time"]
    assert d["wall_first_touch_exchange_event_time"]
    assert d["detection_exchange_event_time"]
    assert d["wall_generation_id"].startswith("wg_")
    assert d["replay_epoch"] is not None
    assert "research_visit_count=3" in d["detector_research_visit_count_note"]


def test_02_wall_flow_inputs_do_not_embed_first_touch_iso(derivation_and_handoff):
    _, h = derivation_and_handoff
    inputs = wall_flow_inputs_from_handoff(h)
    blob = str(inputs)
    assert "FIRST_TOUCH_ISO" not in blob
    assert "1999-01-01" not in blob


def test_03_forbidden_config_does_not_change_handoff_times(derivation_and_handoff):
    der0, h0 = derivation_and_handoff
    der1 = run_detector_stage(cfg={"FIRST_TOUCH_ISO": "1999-01-01T00:00:00Z", "detection": "1999-01-01T00:00:00Z"})
    h1 = handoff_from_derivation(der1)
    assert h0.zone_first_touch_exchange_event_time == h1.zone_first_touch_exchange_event_time
    assert h0.wall_first_touch_exchange_event_time == h1.wall_first_touch_exchange_event_time
    assert h0.detection_exchange_event_time == h1.detection_exchange_event_time
    assert h0.wall_generation_id == h1.wall_generation_id


def test_04_missing_handoff_fail_closed(tmp_path):
    with pytest.raises(HandoffError):
        validate_handoff(None)
    r = run_integrated(run_key="nohandoff", out_dir=tmp_path / "nohandoff", skip_detector=True)
    assert r["ok"] is False
    assert r.get("fail_closed") is True


def test_05_tampered_wall_generation_fail_closed(derivation_and_handoff, tmp_path):
    _, h = derivation_and_handoff
    bad = h.to_dict()
    bad["wall_generation_id"] = "wg_deadbeefdeadbeef"
    with pytest.raises(HandoffError):
        run_wall_flow_from_handoff(
            handoff=validate_handoff(bad),
            persist_dir=book_persist_dir(),
            out_dir=tmp_path / "badgen",
            run_key="badgen",
        )


def test_06_future_trades_do_not_change_earlier_handoff(derivation_and_handoff):
    from obfull_research_engine.level_first_episode1_touch_detection_independent_v1.derive import (
        derive_zone_first_touch,
        derive_detection,
    )
    from obfull_research_engine.level_first_episode1_touch_detection_independent_v1.inputs import (
        load_zone_from_mp_events,
        load_trades,
        load_candles,
    )
    from datetime import timedelta

    _, h0 = derivation_and_handoff
    zone = load_zone_from_mp_events()
    trades = load_trades() + [
        {
            "trade_id": "future-zzz",
            "trade_ts": "2026-09-06T23:00:00Z",
            "price": 79775.0,
            "size": 1.0,
            "taker_side": "Buy",
            "collector_received_at": "2026-09-06T23:00:01Z",
        }
    ]
    candles = load_candles()
    window_end = zone["zone_available_at_dt"] + timedelta(hours=2)
    z = derive_zone_first_touch(zone=zone, trades=trades, candles=candles, window_end=window_end)
    d = derive_detection(zone=zone, zone_touch=z, trades=trades, candles=candles, window_end=window_end)
    assert z["exchange_event_time"] == h0.zone_first_touch_exchange_event_time
    assert d["detected_at"] == h0.detection_exchange_event_time
