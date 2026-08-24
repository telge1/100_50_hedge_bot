"""P6 StrategySpecV2 YAML decoder tests."""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.decoder_v2 import (
    StrategyDecodeError,
    decode_strategy_v2,
    load_strategy_v2_yaml,
)
from orderbook_analyse.strategy_lab.loader import load_strategy_yaml
from orderbook_analyse.strategy_lab.models import StrategySpecV2
from orderbook_analyse.strategy_lab.models.strategy_v2 import STRATEGY_SPEC_V2_SCHEMA_VERSION

REPO = Path(__file__).resolve().parents[2]
EDC_YAML = REPO / "strategies/strategy_lab/edc_m0_strict_sync_v2.yaml"
CLUSTER_YAML = REPO / "strategies/strategy_lab/cluster_sweep_v2.yaml"


def _load_raw(path: Path) -> dict:
    return load_strategy_yaml(path)


def test_decode_edc_and_cluster_yaml_to_strategy_spec_v2() -> None:
    edc = load_strategy_v2_yaml(EDC_YAML.read_text(encoding="utf-8"))
    cluster = load_strategy_v2_yaml(CLUSTER_YAML.read_text(encoding="utf-8"))
    assert isinstance(edc, StrategySpecV2)
    assert isinstance(cluster, StrategySpecV2)
    assert edc.metadata.schema_version == STRATEGY_SPEC_V2_SCHEMA_VERSION
    assert cluster.metadata.schema_version == STRATEGY_SPEC_V2_SCHEMA_VERSION


def test_wrong_schema_version_rejected() -> None:
    data = _load_raw(EDC_YAML)
    data = copy.deepcopy(data)
    data["metadata"]["schema_version"] = "strategy_spec/v1"
    with pytest.raises(StrategyDecodeError, match="schema_version"):
        decode_strategy_v2(data)


def test_unknown_field_rejected() -> None:
    data = copy.deepcopy(_load_raw(EDC_YAML))
    data["metadata"]["extra_field"] = "nope"
    with pytest.raises(StrategyDecodeError, match="unknown field"):
        decode_strategy_v2(data)


def test_missing_required_field_rejected() -> None:
    data = copy.deepcopy(_load_raw(EDC_YAML))
    del data["exit"]["take_profit"]
    with pytest.raises(StrategyDecodeError, match="missing required field"):
        decode_strategy_v2(data)


def test_unknown_kind_rejected() -> None:
    data = copy.deepcopy(_load_raw(EDC_YAML))
    data["signal"]["kind"] = "not_a_signal"
    with pytest.raises(StrategyDecodeError, match="unknown kind"):
        decode_strategy_v2(data)


def test_invalid_enum_rejected() -> None:
    data = copy.deepcopy(_load_raw(EDC_YAML))
    data["entry"]["entry_price_reference"] = "not_a_price_ref"
    with pytest.raises(StrategyDecodeError, match="invalid"):
        decode_strategy_v2(data)


def test_float_instead_of_decimal_rejected() -> None:
    data = copy.deepcopy(_load_raw(EDC_YAML))
    data["exit"]["take_profit"]["value"] = 0.75  # float, not Decimal
    with pytest.raises(StrategyDecodeError, match="Decimal"):
        decode_strategy_v2(data)


def test_bool_as_int_rejected() -> None:
    data = copy.deepcopy(_load_raw(EDC_YAML))
    # features[0].bindings[0].value is IntParam for ema period
    data["features"][0]["bindings"][0]["value"]["value"] = True
    with pytest.raises(StrategyDecodeError, match="bool is not allowed"):
        decode_strategy_v2(data)


def test_sequences_become_tuples() -> None:
    edc = load_strategy_v2_yaml(EDC_YAML.read_text(encoding="utf-8"))
    assert type(edc.features) is tuple
    assert type(edc.data_requirements) is tuple
    assert type(edc.research_parameter_space.dimensions) is tuple
    assert type(edc.analysis_requirements.required_label_fields) is tuple


def test_input_mapping_not_mutated() -> None:
    data = _load_raw(EDC_YAML)
    before = copy.deepcopy(data)
    decode_strategy_v2(data)
    assert data == before


def test_error_path_is_exact() -> None:
    data = copy.deepcopy(_load_raw(EDC_YAML))
    data["features"][1]["bindings"][0]["value"]["value"] = True
    with pytest.raises(StrategyDecodeError) as exc_info:
        decode_strategy_v2(data)
    assert exc_info.value.path == "$.features[1].bindings[0].value.value"


def test_no_v1_migration() -> None:
    data = copy.deepcopy(_load_raw(EDC_YAML))
    data["metadata"]["schema_version"] = "strategy_spec/v1"
    with pytest.raises(StrategyDecodeError, match="V1 migration is not allowed"):
        decode_strategy_v2(data)


def test_missing_exit_is_not_defaulted() -> None:
    data = copy.deepcopy(_load_raw(EDC_YAML))
    del data["exit"]
    with pytest.raises(StrategyDecodeError, match="missing required field"):
        decode_strategy_v2(data)
