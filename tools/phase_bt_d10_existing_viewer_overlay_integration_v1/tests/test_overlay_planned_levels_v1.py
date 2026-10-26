from __future__ import annotations

import sys
from pathlib import Path

BT_D10 = Path(__file__).resolve().parents[1]
if str(BT_D10) not in sys.path:
    sys.path.insert(0, str(BT_D10))

from overlay_export_v1 import export_overlay_bundle


def test_pending_exports_planned_sl_tp():
    integration = {
        "config_hash": "abc",
        "manifest": {"strategy_id": "pool_landscape_v1_candidate", "density_contract": {"min_unique_origins": 2}},
        "signals": [
            {
                "decision_close_utc": "2026-01-01T12:00:00Z",
                "order_active_from_utc": "2026-01-01T12:05:00Z",
                "entry_level": 0.1,
                "pool_lower": 0.1,
                "pool_upper": 0.101,
                "signal_snapshot": {
                    "entry_price_planned": 0.1,
                    "stop_loss_price_planned": 0.1012,
                    "take_profit_price_planned": 0.095,
                    "snapshot_provenance": "LIVE_AT_SIGNAL",
                },
            }
        ],
        "orders": [{"status": "PENDING_PLACED", "signal_close": "2026-01-01T12:00:00Z"}],
        "trades": [],
        "pnl": {},
    }
    bundle = export_overlay_bundle(integration, symbol="DOGEUSDT")
    rec = bundle["records"][0]
    assert rec["sl_price"] == 0.1012
    assert rec["tp_price"] == 0.095
    assert rec["lifecycle_state"] == "PENDING"
