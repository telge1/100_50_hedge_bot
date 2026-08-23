"""Plugin and rule-based signal tests for StrategySpec V2."""

from __future__ import annotations

import dataclasses

import pytest

from orderbook_analyse.strategy_lab.models import (
    Directionality,
    EvaluationTiming,
    PluginKind,
    PluginSignalSpec,
    RuleBasedSignalSpec,
    SideName,
    SideRuleBundle,
)
from tests.strategy_lab.v2_fixtures import (
    _comparison,
    minimal_strategy_spec_v2,
    plugin_signal_v2,
    rule_based_signal_v2,
    sid,
)


def test_plugin_signal_required_fields() -> None:
    signal = plugin_signal_v2()
    required = {
        "plugin",
        "mode_id",
        "directionality",
        "rules_embedded_in_yaml",
        "confirmation_policy",
        "setup",
        "trigger",
        "confirmation",
        "invalidation",
    }
    assert required <= {f.name for f in dataclasses.fields(PluginSignalSpec)}


def test_plugin_rules_embedded_false_allowed() -> None:
    signal = plugin_signal_v2(rules_embedded_in_yaml=False)
    assert signal.rules_embedded_in_yaml is False


def test_plugin_rules_embedded_true_rejected() -> None:
    with pytest.raises(ValueError, match="must be False"):
        plugin_signal_v2(rules_embedded_in_yaml=True)


def test_plugin_directionality_has_no_default() -> None:
    with pytest.raises(TypeError):
        PluginSignalSpec(
            plugin=plugin_signal_v2().plugin,
            mode_id=None,
            rules_embedded_in_yaml=False,
            confirmation_policy=None,
            setup=plugin_signal_v2().setup,
            trigger=plugin_signal_v2().trigger,
            confirmation=plugin_signal_v2().confirmation,
            invalidation=plugin_signal_v2().invalidation,
        )  # type: ignore[call-arg]


def test_plugin_signal_has_no_features() -> None:
    names = {f.name for f in dataclasses.fields(PluginSignalSpec)}
    assert "features" not in names


def test_rule_based_long_only() -> None:
    signal = rule_based_signal_v2(Directionality.LONG)
    assert signal.long is not None
    assert signal.short is None
    assert signal.directionality is Directionality.LONG


def test_rule_based_short_only() -> None:
    signal = rule_based_signal_v2(Directionality.SHORT)
    assert signal.short is not None
    assert signal.long is None


def test_rule_based_both() -> None:
    signal = rule_based_signal_v2(Directionality.BOTH)
    assert signal.long is not None
    assert signal.short is not None


def test_rule_based_no_mirror_fields() -> None:
    names = {f.name for f in dataclasses.fields(RuleBasedSignalSpec)}
    assert "mirror_mode" not in names
    assert "mirror_of" not in names


def test_rule_based_only_signal_bar_close() -> None:
    signal = rule_based_signal_v2()
    assert signal.evaluation_timing is EvaluationTiming.SIGNAL_BAR_CLOSE
    assert list(EvaluationTiming) == [EvaluationTiming.SIGNAL_BAR_CLOSE]


def test_rule_based_components_default_empty() -> None:
    signal = rule_based_signal_v2()
    assert signal.components == ()


def test_v2_root_has_no_long_short() -> None:
    spec = minimal_strategy_spec_v2(signal=rule_based_signal_v2())
    names = {f.name for f in dataclasses.fields(type(spec))}
    assert "long" not in names
    assert "short" not in names


def test_side_rule_bundle_explicit_sides() -> None:
    bundle = SideRuleBundle(
        side=SideName.LONG,
        setup=None,
        trigger=_comparison(),
        confirmation=None,
        invalidation=None,
    )
    assert bundle.side is SideName.LONG
