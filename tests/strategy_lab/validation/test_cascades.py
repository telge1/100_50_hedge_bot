"""P4A error cascade prevention tests."""

from __future__ import annotations

import dataclasses

from orderbook_analyse.strategy_lab.models import (
    ComparisonExpression,
    ContractVersion,
    FeatureOutputReference,
    IntParam,
    PluginRefV2,
    RuleBasedSignalSpec,
    SideName,
    SideRuleBundle,
)
from orderbook_analyse.strategy_lab.models.enums import Directionality, EvaluationTiming
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4a,
)
from tests.strategy_lab.validation.conftest import (
    catalogs,
    edc_features,
    edc_plugin_signal,
    sid,
    valid_edc_strategy,
)


def test_wrong_operator_version_no_signature_cascade(catalogs) -> None:
    trigger = ComparisonExpression(
        operator_id=sid("gt"),
        left=FeatureOutputReference(
            feature_alias=sid("missing"),
            output_id=sid("value"),
        ),
        right=FeatureOutputReference(
            feature_alias=sid("ema_slow"),
            output_id=sid("value"),
        ),
    )
    signal = RuleBasedSignalSpec(
        operator_contract_version=ContractVersion(value="catalog/v1"),
        directionality=Directionality.LONG,
        evaluation_timing=EvaluationTiming.SIGNAL_BAR_CLOSE,
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=trigger,
            confirmation=None,
            invalidation=None,
        ),
        short=None,
    )
    spec = dataclasses.replace(
        valid_edc_strategy(),
        signal=signal,
        features=edc_features(),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.OPERATOR_CONTRACT_VERSION in codes
    assert ValidationIssueCode.OPERATOR_SIGNATURE_MISMATCH not in codes
    assert ValidationIssueCode.OPERAND_UNKNOWN_FEATURE_ALIAS not in codes


def test_unknown_plugin_no_feature_cascade(catalogs) -> None:
    spec = dataclasses.replace(
        valid_edc_strategy(),
        signal=dataclasses.replace(
            edc_plugin_signal(),
            plugin=PluginRefV2(
                plugin_id=sid("missing"),
                contract_version=ContractVersion(value="catalog/v2"),
                config=(),
            ),
        ),
        features=(),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert codes == {ValidationIssueCode.PLUGIN_UNKNOWN}


def test_wrong_plugin_version_no_follow_on_errors(catalogs) -> None:
    spec = dataclasses.replace(
        valid_edc_strategy(),
        signal=dataclasses.replace(
            edc_plugin_signal(),
            plugin=PluginRefV2(
                plugin_id=sid("edc_m0_strict_sync"),
                contract_version=ContractVersion(value="catalog/v1"),
                config=(),
            ),
        ),
        features=(),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert codes == {ValidationIssueCode.PLUGIN_CONTRACT_VERSION}


def test_missing_mode_id_no_missing_config_cascade(catalogs) -> None:
    spec = dataclasses.replace(
        valid_edc_strategy(),
        signal=dataclasses.replace(edc_plugin_signal(), mode_id=None),
    )
    report = validate_strategy_v2_p4a(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.PLUGIN_MODE_MISMATCH in codes
    assert ValidationIssueCode.PLUGIN_MISSING_PARAMETER not in codes


def test_reserved_config_key_no_missing_parameter_cascade(catalogs) -> None:
    from orderbook_analyse.strategy_lab.models.strategy import ConfigEntry

    signal = dataclasses.replace(
        edc_plugin_signal(),
        plugin=PluginRefV2(
            plugin_id=edc_plugin_signal().plugin.plugin_id,
            contract_version=ContractVersion(value="catalog/v2"),
            config=(ConfigEntry(key="confirmation_policy", value=IntParam(value=1)),),
        ),
    )
    spec = dataclasses.replace(valid_edc_strategy(), signal=signal)
    report = validate_strategy_v2_p4a(spec, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.PLUGIN_RESERVED_CONFIG_KEY in codes
    assert ValidationIssueCode.PLUGIN_MISSING_PARAMETER not in codes
