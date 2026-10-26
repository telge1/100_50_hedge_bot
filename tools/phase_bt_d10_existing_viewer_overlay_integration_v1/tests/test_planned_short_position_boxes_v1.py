from __future__ import annotations

import sys
from pathlib import Path

BT_D10 = Path(__file__).resolve().parents[1]
if str(BT_D10) not in sys.path:
    sys.path.insert(0, str(BT_D10))

from overlay_export_v1 import export_overlay_bundle
from trp_position_adapter_v1 import overlay_record_to_position_spec


def test_pending_snapshot_yields_short_position_box():
    integration = {
        "config_hash": "abc",
        "manifest": {"strategy_id": "pool_landscape_v1_candidate", "density_contract": {"min_unique_origins": 2}},
        "signals": [
            {
                "decision_close_utc": "2026-01-01T12:00:00Z",
                "order_active_from_utc": "2026-01-01T12:05:00Z",
                "entry_level": 0.1,
                "signal_snapshot": {
                    "entry_price_planned": 0.1,
                    "stop_loss_price_planned": 0.1012,
                    "take_profit_price_planned": 0.095,
                    "pending_start_utc": "2026-01-01T12:05:00Z",
                },
            }
        ],
        "orders": [{"status": "PENDING_PLACED", "signal_close": "2026-01-01T12:00:00Z"}],
        "trades": [],
        "pnl": {},
    }
    rec = export_overlay_bundle(integration, symbol="DOGEUSDT")["records"][0]
    spec = overlay_record_to_position_spec(rec)
    assert spec and spec["drawing_type"] == "short_position"
    assert spec["metadata"]["plan_only"] is True
