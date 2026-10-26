"""Integration: validated 24h cache pilot (BT-D23)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle"
for p in (
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from setup_scanner_v1 import result_to_json_dict, scan_pool_first_setups  # noqa: E402

BT_D10 = ROOT / "tools/phase_bt_d10_existing_viewer_overlay_integration_v1"
if str(BT_D10) not in sys.path:
    sys.path.insert(0, str(BT_D10))


@pytest.mark.skipif(not CACHE.is_dir(), reason="validated cache bundle missing")
def test_pilot_24h_cache_no_trades_in_output():
    cfg = load_yaml_and_validate(ROOT / "configs/pilot_bt_d21_pool_first_24h.yaml")
    result = scan_pool_first_setups(cfg, run_id="integration_test")
    doc = result_to_json_dict(result)
    assert doc["setup_count"] > 0
    blob = json.dumps(doc)
    assert "net_pnl" not in blob.lower()
    assert '"status": "PLANNED"' in blob or '"status":"PLANNED"' in blob.replace(" ", "")
    for s in doc["setups"]:
        assert s["status"] == "PLANNED"
        assert "planned_entry" in s and "planned_sl" in s


@pytest.mark.skipif(not CACHE.is_dir(), reason="validated cache bundle missing")
def test_chart_overlay_bundle_positions():
    from chart_adapter_v1 import setups_document_to_overlay_bundle
    from trp_position_adapter_v1 import overlay_bundle_to_position_specs

    cfg = load_yaml_and_validate(ROOT / "configs/pilot_bt_d21_pool_first_24h.yaml")
    result = scan_pool_first_setups(cfg, run_id="integration_chart")
    doc = result_to_json_dict(result)
    bundle = setups_document_to_overlay_bundle(doc)
    specs = overlay_bundle_to_position_specs(bundle)
    assert len(specs) == doc["setup_count"]
    assert specs[0]["metadata"]["plan_only"] is True
