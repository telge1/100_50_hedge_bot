"""Feature binding tests for StrategySpec V2."""

from __future__ import annotations

import dataclasses

import pytest

from orderbook_analyse.strategy_lab.models import (
    BoolParam,
    ContractVersion,
    FeatureBindingSpec,
    FeatureOutputReference,
    FeatureParameterBinding,
    PluginSignalSpec,
    RuleBasedSignalSpec,
    StateMachineSignalSpec,
)
from tests.strategy_lab.v2_fixtures import (
    minimal_strategy_spec_v2,
    plugin_signal_v2,
    rule_based_signal_v2,
    sid,
    state_machine_signal_v2,
)


def test_v2_root_is_only_feature_source() -> None:
    spec = minimal_strategy_spec_v2()
    assert isinstance(spec.features, tuple)
    assert len(spec.features) == 1
    root_fields = {f.name for f in dataclasses.fields(type(spec))}
    assert "features" in root_fields
    assert "setup" not in root_fields
    assert "trigger" not in root_fields


@pytest.mark.parametrize(
    "signal",
    (
        plugin_signal_v2(),
        rule_based_signal_v2(),
        state_machine_signal_v2(),
    ),
)
def test_signal_variants_have_no_feature_fields(
    signal: PluginSignalSpec | RuleBasedSignalSpec | StateMachineSignalSpec,
) -> None:
    names = {f.name for f in dataclasses.fields(type(signal))}
    assert "features" not in names
    assert "feature_bindings" not in names


def test_feature_output_reference_requires_output_id() -> None:
    with pytest.raises(TypeError):
        FeatureOutputReference(feature_alias=sid("ema"))  # type: ignore[call-arg]


def test_feature_parameter_binding_uses_param_value() -> None:
    binding = FeatureParameterBinding(
        name=sid("enabled"),
        value=BoolParam(value=True),
    )
    assert isinstance(binding.value, BoolParam)


def test_feature_bindings_reject_lists() -> None:
    with pytest.raises(TypeError):
        FeatureBindingSpec(
            alias=sid("ema"),
            catalog_feature_id=sid("ema"),
            catalog_contract_version=ContractVersion(value="catalog/v1"),
            bindings=[  # type: ignore[arg-type]
                FeatureParameterBinding(
                    name=sid("period"),
                    value=BoolParam(value=True),
                )
            ],
        )
