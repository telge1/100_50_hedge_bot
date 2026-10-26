from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from config_validate_v1 import ConfigValidationError, load_and_validate_config, validate_config_dict

BASELINE = Path(__file__).resolve().parents[1] / "configs/pool_landscape_baseline_v1.json"


def _base() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_baseline_loads():
    cfg = load_and_validate_config(BASELINE)
    assert cfg["short_entry"]["upper_gap_min_pct"] == 0.5


def test_depth_one_fails():
    c = _base()
    c["density"]["min_unique_origins"] = 1
    with pytest.raises(ConfigValidationError) as e:
        validate_config_dict(c)
    assert e.value.code == "DENSITY_MIN_ORIGINS_LOCKED"


def test_unknown_key_fails():
    c = _base()
    c["extra_field"] = 1
    with pytest.raises(ConfigValidationError) as e:
        validate_config_dict(c)
    assert e.value.code == "UNKNOWN_CONFIG_KEYS"


def test_missing_initial_equity_fails():
    c = _base()
    del c["pnl"]["initial_equity_usdt"]
    with pytest.raises(ConfigValidationError):
        validate_config_dict(c)


def test_dense_cluster_without_approval_fails():
    c = _base()
    c["take_profit"]["selection_mode"] = "dense_cluster"
    c["take_profit"]["dense_cluster_semantics_approved"] = False
    with pytest.raises(ConfigValidationError) as e:
        validate_config_dict(c)
    assert e.value.code == "DENSE_CLUSTER_NOT_APPROVED"


def test_gap_only_via_json_value():
    c1 = _base()
    c2 = copy.deepcopy(c1)
    c2["short_entry"]["upper_gap_min_pct"] = 0.75
    validate_config_dict(c1)
    validate_config_dict(c2)
    assert c1["short_entry"]["upper_gap_min_pct"] != c2["short_entry"]["upper_gap_min_pct"]
