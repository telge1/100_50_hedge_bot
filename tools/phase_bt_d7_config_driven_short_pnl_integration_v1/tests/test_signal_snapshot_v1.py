from __future__ import annotations

import json
from pathlib import Path

from config_validate_v1 import load_and_validate_config
from signal_engine_v1 import ShortSignalV1, detect_short_signal
from signal_snapshot_v1 import build_signal_snapshot

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/synthetic_integration_v1.json"
CFG = Path(__file__).resolve().parents[1] / "configs/synthetic_fixture_v1.json"


def test_snapshot_has_planned_sl_tp():
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cfg = load_and_validate_config(CFG)
    close = "2026-01-01T12:05:00Z"
    snap = fix["snapshots"][close]
    density = {
        "upper_bands": snap["upper_bands"],
        "lower_bands": snap["lower_bands"],
        "pipeline_line_id": "test",
        "config_hash": "test",
    }
    sig = detect_short_signal(
        cfg,
        decision_close_utc=close,
        order_active_from_utc="2026-01-01T12:10:00Z",
        candle=fix["prices"][close],
        snapshot=density,
        pool_age_bars={},
    )
    assert sig is not None
    ss = build_signal_snapshot(cfg, sig, density, config_hash="deadbeef" * 4)
    assert ss["entry_price_planned"] == sig.entry_level
    assert ss["stop_loss_price_planned"] > sig.pool_upper
    assert ss["take_profit_price_planned"] is not None
