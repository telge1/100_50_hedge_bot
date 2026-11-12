"""End-to-end single-box replay with temp drawings (no drawings.json mutation)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from research.manual_long_box_research_v1.markers_v1 import build_marker_bundle, report_rows
from research.manual_long_box_research_v1.simulator import replay_long_boxes


def _candle(i: int, o: float, h: float, l: float, c: float, t0: int = 1_700_000_000) -> dict:
    return {"time": t0 + i * 300, "open": o, "high": h, "low": l, "close": c}


def test_doge_single_box_pipeline_synthetic():
    drawing = {
        "drawing_id": "d-test-doge-v1",
        "drawing_type": "long_position",
        "symbol": "DOGEUSDT",
        "visible": True,
        "start_timestamp": 1_700_000_000,
        "end_timestamp": 1_700_010_000,
        "entry_price": 0.0842,
        "stop_price": 0.083,
        "target_price": 0.086,
        "created_on_timeframe": "5m",
    }
    payload = {"version": 1, "drawings": [drawing]}
    candles = [
        _candle(0, 0.085, 0.085, 0.0840, 0.0845),
        _candle(1, 0.0845, 0.085, 0.0835, 0.0846),
        _candle(2, 0.0846, 0.0855, 0.084, 0.0852),
        _candle(3, 0.0852, 0.0865, 0.085, 0.0862),
    ]
    replay = replay_long_boxes(candles, payload, symbol="DOGEUSDT", manual_only=True)
    assert len(replay.boxes) == 1
    assert replay.boxes[0].entry_price == 0.0842
    bundle = build_marker_bundle(replay.boxes[0], replay)
    rows = report_rows(replay.boxes[0], replay)
    assert any(r["event"] == "TOUCH" and r["result"] == "OK" for r in rows)
    assert any(r["event"] == "A2 CONFIRM" for r in rows)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "overlay.json"
        out.write_text(json.dumps(bundle), encoding="utf-8")
        assert out.stat().st_size > 50
