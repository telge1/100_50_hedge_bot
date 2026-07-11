"""Tests for historical pipeline audit harness (no live / entry / TP)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from research.regime_scanner.pipeline_audit import (
    build_pipeline_summary,
    precompute_5m_swings,
    write_pipeline_audit_outputs,
)
from research.regime_scanner.price_action import (
    PriceActionConfig,
    filter_swings_as_of,
    swing_usable_as_of,
)
from research.regime_scanner.point_audit import json_safe


def _tiny_ohlcv(n: int = 40) -> pd.DataFrame:
    start = pd.Timestamp("2026-03-01T00:00:00+00:00")
    rows = []
    # Create a clear high around index 10 and lower high around 25.
    for i in range(n):
        if i == 10:
            h, l, c = 110.0, 100.0, 105.0
        elif i == 25:
            h, l, c = 108.0, 99.0, 103.0
        elif 12 <= i <= 15:
            h, l, c = 102.0, 95.0, 98.0
        else:
            h, l, c = 101.0, 99.0, 100.0
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * i),
                "open": c,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_precompute_swings_gated_by_confirmation_timestamp() -> None:
    frame = _tiny_ohlcv()
    cfg = PriceActionConfig(pivot_left=3, pivot_right=3)
    swings = precompute_5m_swings(frame, pa_config=cfg)
    assert swings
    # Before any confirmation, nothing usable.
    early = filter_swings_as_of(swings, frame.iloc[0]["timestamp"])
    assert early == []
    for swing in swings:
        assert swing_usable_as_of(swing, swing["confirmation_timestamp"])
        assert not swing_usable_as_of(
            swing,
            pd.Timestamp(swing["confirmation_timestamp"]) - pd.Timedelta(minutes=5),
        )


def test_summary_and_outputs_roundtrip(tmp_path: Path) -> None:
    summary = build_pipeline_summary(
        snapshot_rows=[{"index": 0}],
        setup_rows=[
            {
                "setup_id": "setup_00001",
                "setup_side": "short",
                "setup_type": "continuation_weakness",
                "warnings": ["HTF_TRANSITION"],
                "blockers": [],
                "reference_swing_missing": False,
            }
        ],
        event_rows=[
            {"event": "structure_armed", "setup_id": "setup_00001"},
            {"event": "invalidated", "setup_id": "setup_00001"},
        ],
        confirmation_rows=[
            {
                "setup_id": "setup_00001",
                "side": "short",
                "pattern_type": "lower_high",
            }
        ],
        armed_latencies=[3.0, 5.0],
        confirm_latencies=[8.0],
        max_concurrent=1,
        duplicate_confirmations=0,
        duplicate_swing_feeds=0,
        detail_cases={"confirmed_structure": {"pattern_type": "lower_high"}},
        symbol="APTUSDT",
        start="2026-03-01",
        end="2026-03-08",
        elapsed_seconds=1.5,
        pa_config=PriceActionConfig(),
        timeframes="5m,15m,30m",
    )
    assert summary["setup_activations"] == 1
    assert summary["price_action_confirmations"] == 1
    assert summary["price_action_timeframe"] == "5m"
    assert summary["pattern_counts"]["lower_high"] == 1
    assert summary["htf_transition_warnings"] == 1
    assert summary["confirmations_without_structure_armed"] == 0

    payload = {
        "summary": summary,
        "regime_snapshots": [{"index": 0, "combined_regime": "bearish_trend"}],
        "setup_activations": [{"setup_id": "setup_00001", "warnings": ["HTF_TRANSITION"]}],
        "price_action_events": [{"event": "structure_armed"}],
        "price_action_confirmations": [{"side": "short", "pattern_type": "lower_high"}],
    }
    paths = write_pipeline_audit_outputs(payload, tmp_path)
    assert paths["summary_json"].exists()
    assert paths["summary_md"].exists()
    assert paths["confirmations_csv"].exists()
    loaded = json.loads(paths["summary_json"].read_text(encoding="utf-8"))
    assert loaded["symbol"] == "APTUSDT"
    json.dumps(json_safe(payload), allow_nan=False)


def test_confirmation_without_arming_is_counted() -> None:
    summary = build_pipeline_summary(
        snapshot_rows=[],
        setup_rows=[],
        event_rows=[],  # no structure_armed
        confirmation_rows=[{"setup_id": "x", "side": "long", "pattern_type": "higher_low"}],
        armed_latencies=[],
        confirm_latencies=[],
        max_concurrent=0,
        duplicate_confirmations=0,
        duplicate_swing_feeds=0,
        detail_cases={},
        symbol="APTUSDT",
        start=None,
        end=None,
        elapsed_seconds=0.1,
        pa_config=PriceActionConfig(),
        timeframes="5m,15m,30m",
    )
    assert summary["confirmations_without_structure_armed"] == 1
