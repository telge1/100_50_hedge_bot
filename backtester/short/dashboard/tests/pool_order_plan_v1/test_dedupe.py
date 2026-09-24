from __future__ import annotations

from pool_order_plan_v1.dedupe import dedupe_signals
from pool_order_plan_v1.schema import REASON_DUP


def test_dedupe_same_symbol_entry_ignores_tf_and_direction():
    rows = [
        {
            "signal_id": "b",
            "symbol": "hypeusdt",
            "entry_time": "2026-08-11T01:17:00Z",
            "available_at": "2026-08-11T01:16:00Z",
            "created_at": "2026-08-11T01:16:01Z",
            "timeframe": "1h",
            "direction": "SHORT",
        },
        {
            "signal_id": "a",
            "symbol": "HYPEUSDT",
            "entry_time": "2026-08-11T01:17:00Z",
            "available_at": "2026-08-11T01:15:00Z",
            "created_at": "2026-08-11T01:15:01Z",
            "timeframe": "15m",
            "direction": "LONG",
        },
        {
            "signal_id": "c",
            "symbol": "HYPEUSDT",
            "entry_time": "2026-08-11T01:17:00Z",
            "available_at": "2026-08-11T01:15:00Z",
            "created_at": "2026-08-11T01:15:00Z",
            "timeframe": "30m",
            "direction": "LONG",
        },
    ]
    out = dedupe_signals(rows)
    assert len(out["winners"]) == 1
    assert out["winners"][0]["signal_id"] == "c"
    assert len(out["ignored"]) == 2
    assert all(r["no_plan_reason"] == REASON_DUP for r in out["ignored"])
