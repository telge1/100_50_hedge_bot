from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from config_validate_v1 import ConfigValidationError, load_and_validate_config, validate_config_dict
from config_yaml_v1 import load_yaml_and_validate, load_yaml_text
from manifest_v1 import config_content_hash

ROOT = Path(__file__).resolve().parents[3]
BT_D9_JSON = ROOT / "tools/phase_bt_d9_first_24h_short_pnl_v1/configs/bt_d9_24h_v1.json"
CENTRAL_YAML = ROOT / "configs/backtester.yaml"


def test_central_yaml_matches_bt_d9_json_hash():
    json_cfg = load_and_validate_config(BT_D9_JSON)
    yaml_cfg = load_and_validate_config(CENTRAL_YAML)
    yaml_strategy = copy.deepcopy(yaml_cfg)
    yaml_strategy["short_entry"].pop("pending_limit_fill_model", None)
    assert config_content_hash(json_cfg) == config_content_hash(yaml_strategy)


def test_json_path_still_works():
    cfg = load_and_validate_config(BT_D9_JSON)
    assert cfg["backtest"]["symbol"] == "DOGEUSDT"


def test_gap_from_yaml_alias():
    text = CENTRAL_YAML.read_text(encoding="utf-8")
    cfg = load_yaml_text(text)
    assert cfg["short_entry"]["upper_gap_min_pct"] == 0.5


def test_duplicate_yaml_key_fails():
    bad = "yaml_schema_version: bt_d16_yaml_v1\nconfig_version: bt_d6_strategy_backtest_config_v1\nfoo: 1\nfoo: 2\n"
    with pytest.raises(ConfigValidationError) as e:
        load_yaml_text(bad)
    assert e.value.code == "DUPLICATE_YAML_KEY"


def test_unknown_top_level_yaml_key_fails():
    base = yaml_minimal()
    base["unknown_param"] = 1
    with pytest.raises(ConfigValidationError) as e:
        load_yaml_text(dump_minimal(base))
    assert e.value.code == "UNKNOWN_YAML_KEYS"


def test_long_enabled_fails():
    base = yaml_minimal()
    base["long_entry"]["enabled"] = True
    with pytest.raises(ConfigValidationError) as e:
        load_yaml_text(dump_minimal(base))
    assert e.value.code == "LONG_ENTRY_NOT_IMPLEMENTED"


def test_entry_offset_on_lower_edge_mode_fails():
    base = yaml_minimal()
    base["short_entry"]["entry_offset_pct"] = 0.1
    with pytest.raises(ConfigValidationError) as e:
        load_yaml_text(dump_minimal(base))
    assert e.value.code == "ENTRY_MODE_OFFSET_CONFLICT"


def test_entry_offset_offset_mode_ok():
    base = yaml_minimal()
    base["short_entry"]["entry_price_mode"] = "pool_lower_edge_offset"
    base["short_entry"]["entry_offset_pct"] = 0.1
    cfg = load_yaml_text(dump_minimal(base))
    assert cfg["short_entry"]["entry_offset_pct"] == 0.1


def test_yaml_upper_edge_mode():
    base = yaml_minimal()
    base["short_entry"]["entry_price_mode"] = "pool_upper_edge"
    cfg = load_yaml_text(dump_minimal(base))
    assert cfg["short_entry"]["entry_price_reference"] == "upper_pool_upper_edge"


def test_invalid_timestamp_fails():
    base = yaml_minimal()
    base["backtest"]["start_utc"] = "2026-09-11T12:01:00Z"
    with pytest.raises(ConfigValidationError) as e:
        load_yaml_text(dump_minimal(base))
    assert e.value.code == "NOT_5M_ALIGNED"


def test_wrong_depth_fails():
    base = yaml_minimal()
    base["density"]["min_unique_origins"] = 1
    with pytest.raises(ConfigValidationError) as e:
        load_yaml_text(dump_minimal(base))
    assert e.value.code == "DENSITY_MIN_ORIGINS_LOCKED"


def yaml_minimal() -> dict:
    cfg = json.loads(BT_D9_JSON.read_text(encoding="utf-8"))
    return {
        "yaml_schema_version": "bt_d16_yaml_v1",
        "config_version": cfg["config_version"],
        "strategy_identity": cfg["strategy_identity"],
        "density": cfg["density"],
        "short_entry": {
            "entry_price_mode": "pool_lower_edge",
            "entry_offset_pct": None,
            "entry_pool_selection_mode": cfg["short_entry"]["entry_pool_selection_mode"],
            "pool_min_age_bars": 0,
            "gap_min_pct": cfg["short_entry"]["upper_gap_min_pct"],
            "upper_gap_measurement_mode": cfg["short_entry"]["upper_gap_measurement_mode"],
            "pool_proximity_pct": cfg["short_entry"]["upper_pool_proximity_pct"],
            "pending_limit": cfg["short_entry"]["pending_limit_enabled"],
            "pending_limit_expiry_bars": None,
        },
        "long_entry": {"enabled": False},
        "risk": cfg["risk"],
        "take_profit": cfg["take_profit"],
        "pnl": cfg["pnl"],
        "backtest": cfg["backtest"],
    }


def dump_minimal(d: dict) -> str:
    import yaml

    return yaml.dump(d, default_flow_style=False, allow_unicode=True)


def test_window_change_in_yaml():
    base = yaml_minimal()
    base["backtest"]["end_utc"] = "2026-09-11T18:00:00Z"
    cfg = load_yaml_text(dump_minimal(base))
    assert cfg["backtest"]["end_utc"] == "2026-09-11T18:00:00Z"


def test_missing_pnl_section_fails():
    base = yaml_minimal()
    del base["pnl"]
    with pytest.raises(ConfigValidationError) as e:
        load_yaml_text(dump_minimal(base))
    assert e.value.code == "MISSING_YAML_SECTION"


def test_unknown_entry_price_mode_fails():
    base = yaml_minimal()
    base["short_entry"]["entry_price_mode"] = "pool_center_magic"
    with pytest.raises(ConfigValidationError) as e:
        load_yaml_text(dump_minimal(base))
    assert e.value.code == "ENTRY_MODE_NOT_EXECUTABLE"


def test_load_config_with_provenance_yaml():
    from config_yaml_v1 import load_config_with_provenance

    cfg, prov = load_config_with_provenance(CENTRAL_YAML)
    assert prov["format"] == "yaml"
    assert prov["normalized_config_hash"] == config_content_hash(cfg)
