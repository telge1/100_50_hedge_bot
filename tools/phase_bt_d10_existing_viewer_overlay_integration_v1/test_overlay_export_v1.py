"""BT-D10 overlay export tests (ledger parity, no frontend recompute)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]

from overlay_export_v1 import export_overlay_bundle, load_integration_result  # noqa: E402
from trp_position_adapter_v1 import overlay_bundle_to_position_specs, overlay_record_to_position_spec  # noqa: E402

FIXTURE = TOOLS / "fixtures/minimal_closed_trade_v1.json"
HISTORICAL = (
    ROOT
    / "results/phase_bt_d6_config_experiments_v1/historical_pilot_24h_ac6147168271_20261025T102359Z/integration_result.json"
)


def test_export_closed_trade_matches_ledger_prices():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bundle = export_overlay_bundle(raw, integration_path=FIXTURE, symbol="DOGEUSDT")
    assert bundle["contract_version"] in ("bt_d10_overlay_contract_v1", "bt_d10_overlay_contract_v1_1")
    assert len(bundle["records"]) == 1
    rec = bundle["records"][0]
    assert rec["lifecycle_state"] == "CLOSED_TP"
    assert rec["entry_price"] == 0.0845
    assert rec["sl_price"] == raw["trades"][0]["sl_price"]
    assert rec["tp_price"] == raw["trades"][0]["tp_price"]
    assert rec["realized_net_pnl_usdt"] == 9.88
    assert rec["fill_time_utc"] == "2026-09-11T19:50:00Z"
    assert rec["exit_time_utc"] == "2026-09-11T20:10:00Z"


def test_pending_planned_short_position_spec():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["trades"] = []
    raw["pnl"] = {"trade_ledger": []}
    raw["signals"][0]["signal_snapshot"] = {
        "entry_price_planned": 0.0845,
        "stop_loss_price_planned": 0.084687036,
        "take_profit_price_planned": 0.08374,
        "pending_start_utc": "2026-09-11T19:50:00Z",
        "snapshot_provenance": "RECONSTRUCTED",
    }
    bundle = export_overlay_bundle(raw, symbol="DOGEUSDT")
    rec = bundle["records"][0]
    assert rec["lifecycle_state"] == "PENDING"
    spec = overlay_record_to_position_spec(rec)
    assert spec is not None
    assert spec["drawing_type"] == "short_position"
    assert spec["metadata"]["plan_only"] is True
    assert "UNFILLED" in spec["metadata"]["status_label"]
    assert spec["stop"] > spec["entry"] > spec["target"]


def test_trp_adapter_short_position_from_closed_record():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bundle = export_overlay_bundle(raw, symbol="DOGEUSDT")
    specs = overlay_bundle_to_position_specs(bundle)
    assert len(specs) == 1
    assert specs[0]["drawing_type"] == "short_position"
    assert specs[0]["entry"] == 0.0845
    assert specs[0]["stop"] > specs[0]["entry"]
    assert specs[0]["target"] < specs[0]["entry"]


@pytest.mark.skipif(not HISTORICAL.is_file(), reason="historical pilot artifact missing")
def test_historical_pilot_pending_without_snapshot_still_skips_boxes():
    """Legacy pilot rows lack signal_snapshot SL/TP — no invented chart zones."""
    raw = load_integration_result(HISTORICAL)
    bundle = export_overlay_bundle(raw, integration_path=HISTORICAL, symbol="DOGEUSDT")
    assert bundle["records"]
    assert all(r["lifecycle_state"] in ("PENDING", "CANCELLED") for r in bundle["records"])
    assert overlay_bundle_to_position_specs(bundle) == []
