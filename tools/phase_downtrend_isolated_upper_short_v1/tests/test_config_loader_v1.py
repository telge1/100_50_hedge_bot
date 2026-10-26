import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402

from config_loader_v1 import load_isolated_strategy_bundle, load_validated_baseline_config  # noqa: E402


def test_baseline_entry_sl_tp_match_backtester_yaml():
    ref = load_yaml_and_validate(ROOT / "configs/backtester.yaml")
    cfg = load_validated_baseline_config()
    assert cfg["risk"]["sl_above_upper_top_pct"] == ref["risk"]["sl_above_upper_top_pct"] == 0.2
    assert cfg["take_profit"]["min_distance_pct"] == ref["take_profit"]["min_distance_pct"] == 0.5
    assert cfg["short_entry"]["upper_gap_min_pct"] == ref["short_entry"]["upper_gap_min_pct"] == 0.5
    assert cfg["take_profit"]["selection_mode"] == "next_eligible_band"
    assert cfg["density"]["min_unique_origins"] == 2
    assert cfg["density"]["pool_detail"] == 1


def test_isolated_bundle_strategy_id():
    bundle = load_isolated_strategy_bundle()
    assert bundle["isolated_strategy_v1"]["strategy_id"] == "downtrend_isolated_upper_short_v1"
    assert bundle["baseline_config"]["strategy_identity"]["strategy_id"] == "downtrend_isolated_upper_short_v1"
    assert bundle["isolated_strategy_v1"]["regime_classifier"]["ema_order_rule"] == "UNSET"
