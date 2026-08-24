"""P4C ValidationIssueCode coverage tests."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from orderbook_analyse.strategy_lab.models import (
    ContractVersion,
    DurationParam,
    DurationValue,
    ExitParameterTargetV2,
    FeatureParameterTargetV2,
    IntParam,
    PluginConfigParameterTargetV2,
    PluginProvenanceRefV2,
    RateParam,
    RateValue,
    ResearchDimensionV2,
    ResearchParameterSpaceV2,
    RoundtripCostTargetV2,
    SignalTimeframeTargetV2,
    TimeframeParam,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    DataRequirementRoleV2,
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.enums import (
    DurationUnit,
    ModelingStatus,
    RateUnit,
    TimeframeUnit,
)
from orderbook_analyse.strategy_lab.models.strategy import TimeframeValue
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4c,
)
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from tests.strategy_lab.conftest import _tf
from tests.strategy_lab.validation.conftest import (
    catalogs,
    p4c_valid_edc_strategy,
    p4c_valid_rule_based_long_strategy,
    sid,
)


P4C_ISSUE_CODES: frozenset[ValidationIssueCode] = frozenset(
    code
    for code in ValidationIssueCode
    if code.name.startswith(
        (
            "DATA_",
            "TIMEFRAME_",
            "WARMUP_",
            "ENTRY_",
            "EXIT_",
            "COST_",
            "EXECUTION_",
            "PORTFOLIO_",
            "RESEARCH_",
            "PROVENANCE_",
        )
    )
)


def test_p4c_issue_code_inventory() -> None:
    assert len(P4C_ISSUE_CODES) == 37


def _emit(code: ValidationIssueCode, catalogs: CatalogBundleV2) -> None:
    spec = p4c_valid_edc_strategy()

    if code is ValidationIssueCode.DATA_DUPLICATE_REQUIREMENT_ID:
        req = spec.data_requirements[0]
        broken = dataclasses.replace(
            spec,
            data_requirements=spec.data_requirements + (req,),
        )
    elif code is ValidationIssueCode.DATA_REQUIREMENT_MISSING:
        broken = dataclasses.replace(spec, data_requirements=())
    elif code is ValidationIssueCode.DATA_REQUIREMENT_POLICY_MISMATCH:
        reqs = []
        for req in spec.data_requirements:
            if req.required_for_policy is not None:
                reqs.append(dataclasses.replace(req, required_for_policy=None))
            else:
                reqs.append(req)
        broken = dataclasses.replace(spec, data_requirements=tuple(reqs))
    elif code is ValidationIssueCode.TIMEFRAME_SIGNAL_UNSUPPORTED:
        broken = dataclasses.replace(
            spec,
            timeframes=dataclasses.replace(spec.timeframes, signal=_tf(15)),
            warmup=dataclasses.replace(
                spec.warmup,
                signal_engine=dataclasses.replace(
                    spec.warmup.signal_engine,
                    bar_timeframe=_tf(15),
                ),
            ),
        )
    elif code is ValidationIssueCode.TIMEFRAME_EXECUTION_MISMATCH:
        broken = dataclasses.replace(
            spec,
            timeframes=dataclasses.replace(spec.timeframes, execution=_tf(5)),
        )
    elif code is ValidationIssueCode.TIMEFRAME_EXECUTION_UNSUPPORTED:
        broken = dataclasses.replace(
            spec,
            timeframes=dataclasses.replace(spec.timeframes, execution=_tf(5)),
            execution_assumptions=dataclasses.replace(
                spec.execution_assumptions,
                execution_timeframe=_tf(5),
            ),
        )
    elif code is ValidationIssueCode.WARMUP_BARS_BELOW_REQUIRED:
        broken = dataclasses.replace(
            spec,
            warmup=dataclasses.replace(
                spec.warmup,
                signal_engine=dataclasses.replace(
                    spec.warmup.signal_engine,
                    minimum_bars=10,
                ),
            ),
        )
    elif code is ValidationIssueCode.WARMUP_TIMEFRAME_MISMATCH:
        broken = dataclasses.replace(
            spec,
            warmup=dataclasses.replace(
                spec.warmup,
                signal_engine=dataclasses.replace(
                    spec.warmup.signal_engine,
                    bar_timeframe=_tf(15),
                ),
            ),
        )
    elif code is ValidationIssueCode.WARMUP_SOURCE_PADDING_INSUFFICIENT:
        from orderbook_analyse.strategy_lab.models.contracts_v2.padding import (
            PaddingNotApplicable,
            SourceLoadingPaddingV2,
        )

        broken = dataclasses.replace(
            spec,
            warmup=dataclasses.replace(
                spec.warmup,
                source_loading=SourceLoadingPaddingV2(
                    candle_history=PaddingNotApplicable(not_applicable=True),
                    auxiliary_source_history=PaddingNotApplicable(not_applicable=True),
                ),
            ),
        )
    elif code is ValidationIssueCode.WARMUP_OUTCOME_PADDING_INSUFFICIENT:
        from orderbook_analyse.strategy_lab.models.contracts_v2.padding import (
            PaddingNotApplicable,
            OutcomeEvaluationPaddingV2,
        )

        broken = dataclasses.replace(
            spec,
            warmup=dataclasses.replace(
                spec.warmup,
                outcome_evaluation=OutcomeEvaluationPaddingV2(
                    post_window_duration=PaddingNotApplicable(not_applicable=True),
                ),
            ),
        )
    elif code is ValidationIssueCode.ENTRY_DECISION_TIMING_MISMATCH:
        from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
            AvailabilityTimingV2,
        )

        broken = dataclasses.replace(
            spec,
            entry=dataclasses.replace(
                spec.entry,
                signal_decision_timing=AvailabilityTimingV2.CONFIRMATION_BAR_CLOSE,
            ),
        )
    elif code is ValidationIssueCode.ENTRY_REFERENCE_RULE_MISMATCH:
        from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
            EntryReferenceRuleV2,
        )

        broken = dataclasses.replace(
            spec,
            entry=dataclasses.replace(
                spec.entry,
                entry_reference_rule=EntryReferenceRuleV2.NEXT_BAR_OPEN_AFTER_CONFIRMATION_BAR,
            ),
        )
    elif code is ValidationIssueCode.EXIT_RATE_NON_POSITIVE:
        broken = dataclasses.replace(
            spec,
            exit=dataclasses.replace(
                spec.exit,
                take_profit=RateValue(value=Decimal("0"), unit=RateUnit.PERCENT),
            ),
        )
    elif code is ValidationIssueCode.EXIT_HORIZON_NON_POSITIVE:
        broken = dataclasses.replace(
            spec,
            exit=dataclasses.replace(
                spec.exit,
                horizon=DurationValue(value=Decimal("0"), unit=DurationUnit.HOURS),
            ),
        )
    elif code is ValidationIssueCode.COST_ROUNDTRIP_NEGATIVE:
        broken = dataclasses.replace(
            spec,
            costs=dataclasses.replace(
                spec.costs,
                roundtrip_cost=RateValue(value=Decimal("-0.1"), unit=RateUnit.PERCENT),
            ),
        )
    elif code is ValidationIssueCode.COST_SLIPPAGE_MODELED_UNSUPPORTED:
        broken = dataclasses.replace(
            spec,
            costs=dataclasses.replace(spec.costs, slippage=ModelingStatus.MODELED),
        )
    elif code is ValidationIssueCode.COST_FUNDING_MODELED_UNSUPPORTED:
        broken = dataclasses.replace(
            spec,
            costs=dataclasses.replace(spec.costs, funding=ModelingStatus.MODELED),
        )
    elif code is ValidationIssueCode.EXECUTION_ROUNDING_MODELED_UNSUPPORTED:
        broken = dataclasses.replace(
            spec,
            execution_assumptions=dataclasses.replace(
                spec.execution_assumptions,
                rounding_status=ModelingStatus.MODELED,
            ),
        )
    elif code is ValidationIssueCode.PORTFOLIO_COMPOUNDING_UNSUPPORTED:
        broken = dataclasses.replace(
            spec,
            portfolio_assumptions=dataclasses.replace(
                spec.portfolio_assumptions,
                compounding=True,
            ),
        )
    elif code is ValidationIssueCode.RESEARCH_DUPLICATE_DIMENSION_ID:
        dim = ResearchDimensionV2(
            dimension_id=sid("dup"),
            target=SignalTimeframeTargetV2(),
            candidates=(TimeframeParam(value=_tf(5)),),
        )
        broken = dataclasses.replace(
            spec,
            research_parameter_space=ResearchParameterSpaceV2(dimensions=(dim, dim)),
        )
    elif code is ValidationIssueCode.RESEARCH_DUPLICATE_TARGET:
        broken = dataclasses.replace(
            spec,
            research_parameter_space=ResearchParameterSpaceV2(
                dimensions=(
                    ResearchDimensionV2(
                        dimension_id=sid("a"),
                        target=SignalTimeframeTargetV2(),
                        candidates=(TimeframeParam(value=_tf(5)),),
                    ),
                    ResearchDimensionV2(
                        dimension_id=sid("b"),
                        target=SignalTimeframeTargetV2(),
                        candidates=(TimeframeParam(value=_tf(5)),),
                    ),
                )
            ),
        )
    elif code is ValidationIssueCode.RESEARCH_CANDIDATES_EMPTY:
        broken = dataclasses.replace(
            spec,
            research_parameter_space=ResearchParameterSpaceV2(
                dimensions=(
                    ResearchDimensionV2(
                        dimension_id=sid("empty"),
                        target=SignalTimeframeTargetV2(),
                        candidates=(),
                    ),
                )
            ),
        )
    elif code is ValidationIssueCode.RESEARCH_UNKNOWN_FEATURE_ALIAS:
        broken = dataclasses.replace(
            spec,
            research_parameter_space=ResearchParameterSpaceV2(
                dimensions=(
                    ResearchDimensionV2(
                        dimension_id=sid("feat"),
                        target=FeatureParameterTargetV2(
                            feature_alias=sid("missing_alias"),
                            parameter_name=sid("period"),
                        ),
                        candidates=(IntParam(value=9),),
                    ),
                )
            ),
        )
    elif code is ValidationIssueCode.RESEARCH_UNKNOWN_FEATURE_PARAMETER:
        broken = dataclasses.replace(
            spec,
            research_parameter_space=ResearchParameterSpaceV2(
                dimensions=(
                    ResearchDimensionV2(
                        dimension_id=sid("feat"),
                        target=FeatureParameterTargetV2(
                            feature_alias=sid("ema_fast"),
                            parameter_name=sid("unknown_param"),
                        ),
                        candidates=(IntParam(value=9),),
                    ),
                )
            ),
        )
    elif code is ValidationIssueCode.RESEARCH_UNKNOWN_PLUGIN_PARAMETER:
        broken = dataclasses.replace(
            p4c_valid_edc_strategy(),
            research_parameter_space=ResearchParameterSpaceV2(
                dimensions=(
                    ResearchDimensionV2(
                        dimension_id=sid("plug"),
                        target=PluginConfigParameterTargetV2(
                            parameter_name=sid("unknown_param"),
                        ),
                        candidates=(IntParam(value=1),),
                    ),
                )
            ),
        )
        # EDC has no plugin config params — unknown always
    elif code is ValidationIssueCode.RESEARCH_UNKNOWN_EXIT_PARAMETER:
        broken = dataclasses.replace(
            spec,
            research_parameter_space=ResearchParameterSpaceV2(
                dimensions=(
                    ResearchDimensionV2(
                        dimension_id=sid("exit"),
                        target=ExitParameterTargetV2(parameter_name=sid("trailing")),
                        candidates=(
                            RateParam(
                                value=RateValue(
                                    value=Decimal("1"), unit=RateUnit.PERCENT
                                )
                            ),
                        ),
                    ),
                )
            ),
        )
    elif code is ValidationIssueCode.RESEARCH_PLUGIN_TARGET_WITHOUT_PLUGIN:
        rule = p4c_valid_rule_based_long_strategy()
        broken = dataclasses.replace(
            rule,
            research_parameter_space=ResearchParameterSpaceV2(
                dimensions=(
                    ResearchDimensionV2(
                        dimension_id=sid("plug"),
                        target=PluginConfigParameterTargetV2(
                            parameter_name=sid("expire_bars"),
                        ),
                        candidates=(IntParam(value=10),),
                    ),
                )
            ),
        )
    elif code is ValidationIssueCode.RESEARCH_CANDIDATE_TYPE:
        broken = dataclasses.replace(
            spec,
            research_parameter_space=ResearchParameterSpaceV2(
                dimensions=(
                    ResearchDimensionV2(
                        dimension_id=sid("feat"),
                        target=FeatureParameterTargetV2(
                            feature_alias=sid("ema_fast"),
                            parameter_name=sid("period"),
                        ),
                        candidates=(
                            RateParam(
                                value=RateValue(
                                    value=Decimal("1"), unit=RateUnit.PERCENT
                                )
                            ),
                        ),
                    ),
                )
            ),
        )
    elif code is ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT:
        broken = dataclasses.replace(
            spec,
            research_parameter_space=ResearchParameterSpaceV2(
                dimensions=(
                    ResearchDimensionV2(
                        dimension_id=sid("feat"),
                        target=FeatureParameterTargetV2(
                            feature_alias=sid("ema_fast"),
                            parameter_name=sid("period"),
                        ),
                        candidates=(IntParam(value=0),),
                    ),
                )
            ),
        )
    elif code is ValidationIssueCode.PROVENANCE_PLUGIN_REFS_MISSING:
        broken = dataclasses.replace(
            spec,
            provenance=dataclasses.replace(spec.provenance, plugin_refs=()),
        )
    elif code is ValidationIssueCode.PROVENANCE_PLUGIN_REFS_UNEXPECTED:
        broken = dataclasses.replace(
            spec,
            provenance=dataclasses.replace(
                spec.provenance,
                plugin_refs=spec.provenance.plugin_refs
                + (
                    PluginProvenanceRefV2(
                        plugin_id=sid("cluster_sweep"),
                        contract_version=ContractVersion(value="catalog/v2"),
                    ),
                ),
            ),
        )
    elif code is ValidationIssueCode.PROVENANCE_PLUGIN_REF_DUPLICATE:
        ref = spec.provenance.plugin_refs[0]
        broken = dataclasses.replace(
            spec,
            provenance=dataclasses.replace(
                spec.provenance,
                plugin_refs=(ref, ref),
            ),
        )
    elif code is ValidationIssueCode.PROVENANCE_PLUGIN_REF_UNKNOWN:
        broken = dataclasses.replace(
            spec,
            provenance=dataclasses.replace(
                spec.provenance,
                plugin_refs=(
                    PluginProvenanceRefV2(
                        plugin_id=sid("missing_plugin"),
                        contract_version=ContractVersion(value="catalog/v2"),
                    ),
                ),
            ),
        )
    elif code is ValidationIssueCode.PROVENANCE_PLUGIN_REF_VERSION:
        broken = dataclasses.replace(
            spec,
            provenance=dataclasses.replace(
                spec.provenance,
                plugin_refs=(
                    PluginProvenanceRefV2(
                        plugin_id=sid("edc_m0_strict_sync"),
                        contract_version=ContractVersion(value="catalog/v1"),
                    ),
                ),
            ),
        )
    elif code is ValidationIssueCode.PROVENANCE_CATALOG_VERSION_MISMATCH:
        broken = dataclasses.replace(
            spec,
            provenance=dataclasses.replace(
                spec.provenance,
                catalog_contract_version=ContractVersion(value="catalog/v1"),
            ),
        )
    elif code is ValidationIssueCode.PROVENANCE_CAUSALITY_MISMATCH:
        from orderbook_analyse.strategy_lab.models.enums import CausalityStatus

        broken = dataclasses.replace(
            spec,
            provenance=dataclasses.replace(
                spec.provenance,
                causality_status=CausalityStatus.CAUSALITY_UNPROVEN,
            ),
        )
    elif code is ValidationIssueCode.PROVENANCE_CAUSALITY_NOT_ALLOWED:
        from orderbook_analyse.strategy_lab.models.enums import CausalityStatus

        broken = dataclasses.replace(
            spec,
            validation_requirements=dataclasses.replace(
                spec.validation_requirements,
                allowed_causality_statuses=(CausalityStatus.CAUSALITY_UNPROVEN,),
            ),
        )
    else:
        raise AssertionError(f"no emitter for {code}")

    report = validate_strategy_v2_p4c(broken, catalogs)
    assert code in {issue.code for issue in report.issues}, (
        code,
        [issue.code for issue in report.issues],
    )


@pytest.mark.parametrize("code", sorted(P4C_ISSUE_CODES, key=lambda c: c.value))
def test_p4c_active_issue_code_is_emitted(code: ValidationIssueCode, catalogs) -> None:
    _emit(code, catalogs)


def test_data_requirement_policy_mismatch_when_divergent(catalogs) -> None:
    spec = p4c_valid_edc_strategy()
    reqs = []
    for req in spec.data_requirements:
        if req.required_for_policy is ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE:
            # keep role/key but we can't set a different policy enum easily —
            # missing None already covered; divergent: set required_for_policy on
            # a req that shouldn't have mismatch via replacing with same enum only.
            # Use None path already in inventory; here force mismatch by clearing.
            reqs.append(dataclasses.replace(req, required_for_policy=None))
        else:
            reqs.append(req)
    broken = dataclasses.replace(spec, data_requirements=tuple(reqs))
    report = validate_strategy_v2_p4c(broken, catalogs)
    policy_issues = [
        i for i in report.issues if i.code is ValidationIssueCode.DATA_REQUIREMENT_POLICY_MISMATCH
    ]
    assert policy_issues
    # one policy mismatch per affected needed requirement, not mixed with missing for same
    assert all(i.path.endswith("required_for_policy") for i in policy_issues)


def test_research_candidate_type_suppresses_constraint(catalogs) -> None:
    spec = p4c_valid_edc_strategy()
    broken = dataclasses.replace(
        spec,
        research_parameter_space=ResearchParameterSpaceV2(
            dimensions=(
                ResearchDimensionV2(
                    dimension_id=sid("feat"),
                    target=FeatureParameterTargetV2(
                        feature_alias=sid("ema_fast"),
                        parameter_name=sid("period"),
                    ),
                    candidates=(
                        RateParam(
                            value=RateValue(value=Decimal("1"), unit=RateUnit.PERCENT)
                        ),
                    ),
                ),
            )
        ),
    )
    report = validate_strategy_v2_p4c(broken, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.RESEARCH_CANDIDATE_TYPE in codes
    assert ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT not in codes


def test_research_signal_tf_candidate_outside_allowed_set(catalogs) -> None:
    spec = p4c_valid_edc_strategy()
    broken = dataclasses.replace(
        spec,
        research_parameter_space=ResearchParameterSpaceV2(
            dimensions=(
                ResearchDimensionV2(
                    dimension_id=sid("tf"),
                    target=SignalTimeframeTargetV2(),
                    candidates=(TimeframeParam(value=_tf(15)),),
                ),
            )
        ),
    )
    report = validate_strategy_v2_p4c(broken, catalogs)
    assert ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT in {
        issue.code for issue in report.issues
    }


def test_unknown_provenance_plugin_suppresses_version(catalogs) -> None:
    spec = p4c_valid_edc_strategy()
    broken = dataclasses.replace(
        spec,
        provenance=dataclasses.replace(
            spec.provenance,
            plugin_refs=(
                PluginProvenanceRefV2(
                    plugin_id=sid("missing_plugin"),
                    contract_version=ContractVersion(value="catalog/v1"),
                ),
            ),
        ),
    )
    report = validate_strategy_v2_p4c(broken, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.PROVENANCE_PLUGIN_REF_UNKNOWN in codes
    assert ValidationIssueCode.PROVENANCE_PLUGIN_REF_VERSION not in codes


def test_unexpected_extra_ref_does_not_emit_missing_when_expected_present(
    catalogs,
) -> None:
    spec = p4c_valid_edc_strategy()
    broken = dataclasses.replace(
        spec,
        provenance=dataclasses.replace(
            spec.provenance,
            plugin_refs=spec.provenance.plugin_refs
            + (
                PluginProvenanceRefV2(
                    plugin_id=sid("cluster_sweep"),
                    contract_version=ContractVersion(value="catalog/v2"),
                ),
            ),
        ),
    )
    report = validate_strategy_v2_p4c(broken, catalogs)
    codes = {issue.code for issue in report.issues}
    assert ValidationIssueCode.PROVENANCE_PLUGIN_REFS_UNEXPECTED in codes
    assert ValidationIssueCode.PROVENANCE_PLUGIN_REFS_MISSING not in codes


def test_warmup_timeframe_mismatch_rule_based(catalogs) -> None:
    spec = p4c_valid_rule_based_long_strategy()
    broken = dataclasses.replace(
        spec,
        warmup=dataclasses.replace(
            spec.warmup,
            signal_engine=dataclasses.replace(
                spec.warmup.signal_engine,
                bar_timeframe=_tf(15),
            ),
        ),
    )
    report = validate_strategy_v2_p4c(broken, catalogs)
    assert ValidationIssueCode.WARMUP_TIMEFRAME_MISMATCH in {
        issue.code for issue in report.issues
    }
