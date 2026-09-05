"""EMA Zone Microstructure Confirmation V1 — candidate_discovery StrategySpec V2."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.catalogs.v2.features import FEATURE_DESCRIPTORS_V2
from orderbook_analyse.strategy_lab.catalogs.v2.models import (
    CandidatePluginDescriptorV2,
    PluginDescriptorV2,
)
from orderbook_analyse.strategy_lab.catalogs.v2.plugins import PLUGIN_DESCRIPTORS_V2
from orderbook_analyse.strategy_lab.catalogs.v2.registry import get_plugin_v2
from orderbook_analyse.strategy_lab.compiler_v2 import (
    StrategyCompilationError,
    compile_candidate_discovery_v2,
    compile_strategy_v2,
)
from orderbook_analyse.strategy_lab.decoder_v2 import (
    StrategyDecodeError,
    load_compile_candidate_discovery_v2,
    load_strategy_v2_yaml_file,
)
from orderbook_analyse.strategy_lab.loader import load_strategy_yaml_path
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    AdapterBindingStatusV2,
    DataSourceKindV2,
    PluginContractStatusV2,
    StrategyRunIntentV2,
)
from orderbook_analyse.strategy_lab.models.strategy_v2 import (
    CandidateDiscoveryStrategySpecV2,
    TradeBacktestStrategySpecV2,
)
from orderbook_analyse.strategy_lab.schema import COMMITTED_SCHEMA_V2_PATH
from orderbook_analyse.strategy_lab.schema.generator import generate_strategy_spec_v2_schema
from orderbook_analyse.strategy_lab.validation import (
    production_catalog_bundle_v2,
    validate_candidate_discovery_v2,
)

REPO = Path(__file__).resolve().parents[2]
STRATEGIES = REPO / "strategies" / "strategy_lab"
EZM_YAML = STRATEGIES / "ema_zone_microstructure_confirmation_v1.yaml"
EDC_YAML = STRATEGIES / "edc_m0_strict_sync_v2.yaml"
CLUSTER_YAML = STRATEGIES / "cluster_sweep_v2.yaml"

FEATURE_IDS = {d.feature_id.value for d in FEATURE_DESCRIPTORS_V2}
SOURCE_KINDS = {k.value for k in DataSourceKindV2}

EXPECTED_STATES = {
    "watch_zone",
    "block_flat_compression",
    "wait_microstructure_confirmation",
    "defense_rejection_confirmed",
    "breakout_confirmed",
    "false_breakout_confirmed",
    "wait_next_zone_confirmation",
    "possible_regime_flip",
    "full_regime_flip_confirmed",
    "no_trade",
    "data_incomplete",
}


@pytest.fixture
def catalogs():
    return production_catalog_bundle_v2()


def test_existing_trade_specs_remain_valid(catalogs) -> None:
    for path in (EDC_YAML, CLUSTER_YAML):
        spec = load_strategy_v2_yaml_file(path)
        assert isinstance(spec, TradeBacktestStrategySpecV2)
        assert spec.run_intent is StrategyRunIntentV2.TRADE_BACKTEST
        compiled = compile_strategy_v2(spec, catalogs)
        assert len(compiled.strategy_hash) == 64


def test_missing_run_intent_defaults_to_trade_backtest(catalogs) -> None:
    raw = load_strategy_yaml_path(EDC_YAML)
    assert "run_intent" not in raw
    spec = load_strategy_v2_yaml_file(EDC_YAML)
    assert spec.run_intent is StrategyRunIntentV2.TRADE_BACKTEST


def test_ezm_candidate_spec_loads_validates_and_compiles(catalogs) -> None:
    spec = load_strategy_v2_yaml_file(EZM_YAML)
    assert isinstance(spec, CandidateDiscoveryStrategySpecV2)
    assert spec.run_intent is StrategyRunIntentV2.CANDIDATE_DISCOVERY
    report = validate_candidate_discovery_v2(spec, catalogs)
    assert report.is_valid, report.issues
    compiled = load_compile_candidate_discovery_v2(EZM_YAML, catalogs)
    assert compiled.plugin_id == "ema_zone_microstructure_confirmation"
    assert set(compiled.candidate_states) == EXPECTED_STATES
    assert "ezm_orderbook_ob200_v3_raw" in compiled.data_requirement_ids
    assert "ezm_public_trades_native" in compiled.data_requirement_ids


def test_ezm_cannot_trade_backtest_compile(catalogs) -> None:
    spec = load_strategy_v2_yaml_file(EZM_YAML)
    with pytest.raises(StrategyCompilationError, match="CANDIDATE_DISCOVERY_NOT_TRADE_BACKTEST"):
        compile_strategy_v2(spec, catalogs)


def test_trade_spec_without_execution_still_invalid() -> None:
    raw = load_strategy_yaml_path(EDC_YAML)
    raw = dict(raw)
    del raw["exit"]
    with pytest.raises(StrategyDecodeError):
        from orderbook_analyse.strategy_lab.decoder_v2 import decode_strategy_v2

        decode_strategy_v2(raw)


def test_candidate_spec_with_trade_execution_invalid() -> None:
    raw = load_strategy_yaml_path(EZM_YAML)
    raw = dict(raw)
    raw["exit"] = {
        "take_profit": {"unit": "percent", "value": Decimal("0.75")},
        "stop_loss": {"unit": "percent", "value": Decimal("0.5")},
        "horizon": {"unit": "hours", "value": Decimal("8")},
    }
    with pytest.raises(StrategyDecodeError, match="forbids field 'exit'"):
        from orderbook_analyse.strategy_lab.decoder_v2 import decode_strategy_v2

        decode_strategy_v2(raw)


def test_candidate_plugin_has_no_entry_enums() -> None:
    plugin = get_plugin_v2("ema_zone_microstructure_confirmation")
    assert isinstance(plugin, CandidatePluginDescriptorV2)
    assert not hasattr(plugin, "entry_reference_rule")
    assert plugin.contract_status is PluginContractStatusV2.RESEARCH_CONTRACT_ONLY
    assert set(s.value for s in plugin.candidate_states) == EXPECTED_STATES


def test_trade_plugin_still_requires_entry_semantics() -> None:
    plugin = get_plugin_v2("edc_m0_strict_sync")
    assert isinstance(plugin, PluginDescriptorV2)
    assert plugin.entry_reference_rule is not None
    assert plugin.entry_timing_anchor is not None
    assert plugin.entry_price_reference is not None


def test_raw_ob200_distinct_from_1m_aggregate() -> None:
    assert "orderbook_ob200_v3_1m" in SOURCE_KINDS
    assert "orderbook_ob200_v3_raw" in SOURCE_KINDS
    assert "orderbook_ob200_v3_raw" != "orderbook_ob200_v3_1m"


def test_public_trades_native_distinct_from_1m() -> None:
    assert "public_trades_1m" in SOURCE_KINDS
    assert "public_trades_native" in SOURCE_KINDS
    assert "public_trades_native" != "public_trades_1m"


def test_ezm_has_no_entry_exit_tp_sl(catalogs) -> None:
    spec = load_strategy_v2_yaml_file(EZM_YAML)
    assert not hasattr(spec, "entry")
    assert not hasattr(spec, "exit")
    assert not hasattr(spec, "costs")
    assert not hasattr(spec, "portfolio_assumptions")
    assert not hasattr(spec, "execution_assumptions")
    text = EZM_YAML.read_text(encoding="utf-8")
    for banned in ("take_profit", "stop_loss", "entry:", "exit:", "fixed_notional"):
        assert banned not in text


def test_ema200_not_equal_weight_regime(catalogs) -> None:
    spec = load_strategy_v2_yaml_file(EZM_YAML)
    by_alias = {b.alias.value: b for b in spec.features}
    assert by_alias["ema_fast"].bindings[0].value.value == 9
    assert by_alias["ema_medium"].bindings[0].value.value == 20
    assert by_alias["ema_slow"].bindings[0].value.value == 59
    assert by_alias["ema_structure"].bindings[0].value.value == 200
    assert "EMA200" in spec.metadata.description or "ema200" in spec.metadata.description.lower()
    assert "not equal-weight" in spec.metadata.description.lower() or "superior" in spec.metadata.description.lower()


def test_feature_and_data_identifiers(catalogs) -> None:
    spec = load_strategy_v2_yaml_file(EZM_YAML)
    for binding in spec.features:
        assert binding.catalog_feature_id.value in FEATURE_IDS
    for req in spec.data_requirements:
        assert req.source_kind.value in SOURCE_KINDS


def test_decimal_semantics_preserved() -> None:
    raw = load_strategy_yaml_path(EZM_YAML)
    # Safe loader: !!float becomes Decimal
    hist = raw["warmup"]["source_loading"]["candle_history"]["value"]
    assert type(hist) is Decimal


def test_schema_python_parity_includes_candidate_root() -> None:
    schema = generate_strategy_spec_v2_schema()
    defs = schema["$defs"]
    assert "TradeBacktestStrategySpecV2" in defs
    assert "CandidateDiscoveryStrategySpecV2" in defs
    assert schema["$ref"] == "#/$defs/StrategySpecV2"
    assert "oneOf" in defs["StrategySpecV2"]
    committed = COMMITTED_SCHEMA_V2_PATH.read_text(encoding="utf-8")
    assert "candidate_discovery" in committed
    assert "orderbook_ob200_v3_raw" in committed
    assert "public_trades_native" in committed


def test_catalog_only_adapter_status_still_exists() -> None:
    assert AdapterBindingStatusV2.CATALOG_ONLY.value == "catalog_only"
    assert any(isinstance(p, CandidatePluginDescriptorV2) for p in PLUGIN_DESCRIPTORS_V2)
