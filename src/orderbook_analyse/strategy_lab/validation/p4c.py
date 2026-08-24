"""P4C validation entry points for StrategySpecV2 root contracts."""

from __future__ import annotations

from decimal import Decimal

from orderbook_analyse.strategy_lab.catalogs.v2.models import (
    CATALOG_CONTRACT_VERSION,
    FeatureDescriptorV2,
    PluginDescriptorV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.data_requirement import (
    DataRequirementSpecV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    AvailabilityTimingV2,
    EntryReferenceRuleV2,
    FeatureWarmupFormulaKindV2,
    ParameterValueType,
    SignalTimeframeModeV2,
    WarmupTimeframeBasisV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.granularity import (
    EventStreamGranularityV2,
    SelectedSignalTimeframeGranularityV2,
    SnapshotGranularityV2,
    TimeframeGranularityV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.padding import (
    PaddingNotApplicable,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.param_mapping import (
    param_value_to_parameter_type,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.phase1_contracts import (
    ExitParameterTargetV2,
    FeatureParameterTargetV2,
    PluginConfigParameterTargetV2,
    RoundtripCostTargetV2,
    SignalTimeframeTargetV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.feature import ParameterDefinitionV2
from orderbook_analyse.strategy_lab.models.enums import (
    DurationUnit,
    ModelingStatus,
    RateUnit,
    TimeframeUnit,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.signals import PluginSignalSpec
from orderbook_analyse.strategy_lab.models.strategy import (
    DurationParam,
    DurationValue,
    IntParam,
    ParamValue,
    RateParam,
    TimeframeParam,
    TimeframeValue,
)
from orderbook_analyse.strategy_lab.models.strategy_v2 import StrategySpecV2
from orderbook_analyse.strategy_lab.validation._issue_helpers import make_error
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.diagnostics import (
    ExpectedActualTypeContext,
    ExpectedActualVersionContext,
    ParameterNameContext,
    UnknownIdentifierContext,
)
from orderbook_analyse.strategy_lab.validation.features import (
    FeatureResolutionIndex,
    build_feature_resolution_index,
)
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationFailedError,
    ValidationIssue,
    ValidationIssueCode,
    ValidationReport,
    build_report,
)
from orderbook_analyse.strategy_lab.validation.p4b import validate_strategy_v2_p4b
from orderbook_analyse.strategy_lab.validation.parameters import (
    config_parameter_definitions,
    remap_parameter_issues_to_research,
    validate_parameter_value,
)


def validate_strategy_v2_p4c(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
) -> ValidationReport:
    """Validate P4A + P4B + P4C scope: graph plus root contracts."""
    issues: list[ValidationIssue] = list(validate_strategy_v2_p4b(spec, catalogs).issues)
    p4a_codes = {issue.code for issue in issues}
    index = build_feature_resolution_index(spec.features, catalogs)
    plugin = _resolve_plugin(spec, catalogs, p4a_codes)

    issues.extend(_validate_timeframes(spec, plugin))
    issues.extend(_validate_data_requirements(spec, catalogs, index, plugin, p4a_codes))
    issues.extend(_validate_warmup(spec, index, plugin))
    issues.extend(_validate_entry(spec, plugin))
    issues.extend(_validate_exit(spec))
    issues.extend(_validate_execution_costs_portfolio(spec))
    issues.extend(_validate_research_space(spec, catalogs, index, plugin, p4a_codes))
    issues.extend(_validate_provenance(spec, catalogs, plugin, p4a_codes))
    return build_report(tuple(issues))


def require_valid_strategy_v2_p4c(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
) -> None:
    """Raise ValidationFailedError when P4C validation produces errors."""
    report = validate_strategy_v2_p4c(spec, catalogs)
    if not report.is_valid:
        raise ValidationFailedError(report)


def _resolve_plugin(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
    p4a_codes: set[ValidationIssueCode],
) -> PluginDescriptorV2 | None:
    if not isinstance(spec.signal, PluginSignalSpec):
        return None
    if ValidationIssueCode.PLUGIN_UNKNOWN in p4a_codes:
        return None
    if ValidationIssueCode.PLUGIN_CONTRACT_VERSION in p4a_codes:
        return None
    try:
        return catalogs.plugins.get(spec.signal.plugin.plugin_id.value)
    except Exception:
        return None


def _signal_minutes(spec: StrategySpecV2) -> int | None:
    tf = spec.timeframes.signal
    if tf.unit is not TimeframeUnit.MINUTES:
        return None
    return tf.value


def _tf_minutes(tf: TimeframeValue) -> int | None:
    if tf.unit is not TimeframeUnit.MINUTES:
        return None
    return tf.value


def _granularity_key(
    granularity: object,
    *,
    signal_minutes: int | None,
) -> tuple[object, ...] | None:
    if isinstance(granularity, SelectedSignalTimeframeGranularityV2):
        if signal_minutes is None:
            return None
        return ("timeframe", signal_minutes)
    if isinstance(granularity, TimeframeGranularityV2):
        minutes = _tf_minutes(granularity.timeframe)
        if minutes is None:
            return None
        return ("timeframe", minutes)
    if isinstance(granularity, SnapshotGranularityV2):
        minutes = _tf_minutes(granularity.aligned_timeframe)
        if minutes is None:
            return None
        return ("snapshot", minutes)
    if isinstance(granularity, EventStreamGranularityV2):
        return ("event_stream",)
    return None


def _coverage_key(
    requirement: DataRequirementSpecV2,
    *,
    signal_minutes: int | None,
) -> tuple[object, ...] | None:
    gran = _granularity_key(requirement.granularity, signal_minutes=signal_minutes)
    if gran is None:
        return None
    return (
        requirement.source_kind.value,
        gran,
        requirement.availability.value,
        requirement.role.value,
    )


def _validate_timeframes(
    spec: StrategySpecV2,
    plugin: PluginDescriptorV2 | None,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    signal_minutes = _signal_minutes(spec)
    exec_root = spec.timeframes.execution
    exec_assumptions = spec.execution_assumptions.execution_timeframe

    if (
        _tf_minutes(exec_root) != _tf_minutes(exec_assumptions)
        or exec_root.unit is not exec_assumptions.unit
    ):
        issues.append(
            make_error(
                ValidationIssueCode.TIMEFRAME_EXECUTION_MISMATCH,
                path="timeframes.execution",
                message=(
                    "timeframes.execution must equal "
                    "execution_assumptions.execution_timeframe"
                ),
                context=ExpectedActualVersionContext(
                    expected=_tf_label(exec_assumptions),
                    actual=_tf_label(exec_root),
                ),
            )
        )

    if _tf_minutes(exec_assumptions) != 1 or exec_assumptions.unit is not TimeframeUnit.MINUTES:
        issues.append(
            make_error(
                ValidationIssueCode.TIMEFRAME_EXECUTION_UNSUPPORTED,
                path="execution_assumptions.execution_timeframe",
                message="phase-1 execution timeframe must be 1 minutes",
                context=ExpectedActualVersionContext(
                    expected="1minutes",
                    actual=_tf_label(exec_assumptions),
                ),
            )
        )

    if plugin is not None and signal_minutes is not None:
        contract = plugin.signal_timeframe
        if contract.mode is SignalTimeframeModeV2.FIXED:
            if signal_minutes != contract.reference_minutes:
                issues.append(
                    make_error(
                        ValidationIssueCode.TIMEFRAME_SIGNAL_UNSUPPORTED,
                        path="timeframes.signal",
                        message=(
                            f"plugin requires fixed signal timeframe "
                            f"{contract.reference_minutes} minutes"
                        ),
                        context=ExpectedActualVersionContext(
                            expected=str(contract.reference_minutes),
                            actual=str(signal_minutes),
                        ),
                    )
                )
        elif contract.mode is SignalTimeframeModeV2.ALLOWED_SET:
            if signal_minutes not in contract.allowed_minutes:
                issues.append(
                    make_error(
                        ValidationIssueCode.TIMEFRAME_SIGNAL_UNSUPPORTED,
                        path="timeframes.signal",
                        message=(
                            "signal timeframe is not in plugin allowed_minutes "
                            f"{contract.allowed_minutes!r}"
                        ),
                        context=ExpectedActualVersionContext(
                            expected=",".join(str(v) for v in contract.allowed_minutes),
                            actual=str(signal_minutes),
                        ),
                    )
                )
    return tuple(issues)


def _tf_label(tf: TimeframeValue) -> str:
    return f"{tf.value}{tf.unit.value}"


def _validate_data_requirements(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
    index: FeatureResolutionIndex,
    plugin: PluginDescriptorV2 | None,
    p4a_codes: set[ValidationIssueCode],
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    signal_minutes = _signal_minutes(spec)
    seen_ids: set[str] = set()

    for i, requirement in enumerate(spec.data_requirements):
        rid = requirement.requirement_id.value
        if rid in seen_ids:
            issues.append(
                make_error(
                    ValidationIssueCode.DATA_DUPLICATE_REQUIREMENT_ID,
                    path=f"data_requirements[{i}].requirement_id",
                    message=f"duplicate data requirement id {rid!r}",
                    context=UnknownIdentifierContext(
                        identifier=requirement.requirement_id
                    ),
                )
            )
        seen_ids.add(rid)

    strategy_entries: list[tuple[int, DataRequirementSpecV2, tuple[object, ...]]] = []
    for i, requirement in enumerate(spec.data_requirements):
        key = _coverage_key(requirement, signal_minutes=signal_minutes)
        if key is not None:
            strategy_entries.append((i, requirement, key))

    needed: list[tuple[DataRequirementSpecV2, str]] = []
    if plugin is not None:
        for requirement in plugin.data_requirements:
            needed.append((requirement, f"plugin:{plugin.plugin_id.value}"))

    feature_errors = {
        ValidationIssueCode.FEATURE_UNKNOWN_ID,
        ValidationIssueCode.FEATURE_CONTRACT_VERSION,
        ValidationIssueCode.FEATURE_DUPLICATE_ALIAS,
    }
    features_resolvable = not (p4a_codes & feature_errors)
    if features_resolvable:
        for binding in spec.features:
            if not index.has_alias(binding.alias):
                continue
            try:
                feature = catalogs.features.get(binding.catalog_feature_id.value)
            except Exception:
                continue
            for requirement in feature.data_requirements:
                needed.append((requirement, f"feature:{feature.feature_id.value}"))

    covered_needed: set[int] = set()
    for needed_index, (needed_req, _origin) in enumerate(needed):
        needed_key = _coverage_key(needed_req, signal_minutes=signal_minutes)
        if needed_key is None:
            continue
        match: tuple[int, DataRequirementSpecV2] | None = None
        for strategy_index, strategy_req, strategy_key in strategy_entries:
            if strategy_key != needed_key:
                continue
            if needed_req.required and not strategy_req.required:
                continue
            match = (strategy_index, strategy_req)
            break
        if match is None:
            issues.append(
                make_error(
                    ValidationIssueCode.DATA_REQUIREMENT_MISSING,
                    path="data_requirements",
                    message=(
                        "missing data requirement coverage for "
                        f"source={needed_req.source_kind.value!r} "
                        f"role={needed_req.role.value!r} "
                        f"availability={needed_req.availability.value!r}"
                    ),
                    context=None,
                )
            )
            continue
        strategy_index, strategy_req = match
        if needed_req.required_for_policy is not None:
            if (
                strategy_req.required_for_policy is None
                or strategy_req.required_for_policy is not needed_req.required_for_policy
            ):
                expected = needed_req.required_for_policy.value
                actual = (
                    "none"
                    if strategy_req.required_for_policy is None
                    else strategy_req.required_for_policy.value
                )
                issues.append(
                    make_error(
                        ValidationIssueCode.DATA_REQUIREMENT_POLICY_MISMATCH,
                        path=(
                            f"data_requirements[{strategy_index}]"
                            ".required_for_policy"
                        ),
                        message=(
                            "data requirement policy must match needed "
                            f"required_for_policy {expected!r}"
                        ),
                        context=ExpectedActualVersionContext(
                            expected=expected,
                            actual=actual,
                        ),
                    )
                )
    return tuple(issues)


def _feature_warmup_bars(
    binding_alias: str,
    binding_values: dict[str, ParamValue],
    feature: FeatureDescriptorV2,
) -> tuple[int, str] | None:
    """Return (bars, source_label) for the strongest resolvable output warmup."""
    best: tuple[int, str] | None = None
    for output in feature.outputs:
        formula = output.warmup
        if formula.formula_kind is FeatureWarmupFormulaKindV2.NO_SEPARATE_BAR_GATE:
            continue
        if formula.formula_kind is FeatureWarmupFormulaKindV2.PLUGIN_SIGNAL_GATE:
            continue
        if formula.formula_kind is FeatureWarmupFormulaKindV2.BARS_FROM_PARAMETER:
            if formula.parameter_name is None:
                continue
            value = binding_values.get(formula.parameter_name.value)
            if not isinstance(value, IntParam):
                continue
            bars = value.value
            label = f"feature:{binding_alias}.{formula.parameter_name.value}"
            if best is None or bars > best[0]:
                best = (bars, label)
    return best


def _duration_hours(value: DurationValue) -> Decimal | None:
    if value.unit is DurationUnit.HOURS:
        return value.value
    if value.unit is DurationUnit.MINUTES:
        return value.value / Decimal("60")
    return None


def _padding_covers(
    strategy_pad: DurationValue | PaddingNotApplicable,
    needed_pad: DurationValue | PaddingNotApplicable,
) -> bool:
    if isinstance(needed_pad, PaddingNotApplicable):
        return True
    if isinstance(strategy_pad, PaddingNotApplicable):
        return False
    needed_hours = _duration_hours(needed_pad)
    strategy_hours = _duration_hours(strategy_pad)
    if needed_hours is None or strategy_hours is None:
        return strategy_pad.unit is needed_pad.unit and strategy_pad.value >= needed_pad.value
    return strategy_hours >= needed_hours


def _validate_warmup(
    spec: StrategySpecV2,
    index: FeatureResolutionIndex,
    plugin: PluginDescriptorV2 | None,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    required_bars = 0
    driving_source = "none"

    for binding in spec.features:
        resolved = index.get_binding_and_feature(binding.alias)
        if resolved is None:
            continue
        _binding, feature = resolved
        values = {item.name.value: item.value for item in binding.bindings}
        result = _feature_warmup_bars(binding.alias.value, values, feature)
        if result is not None and result[0] > required_bars:
            required_bars, driving_source = result

    if plugin is not None:
        plugin_bars = plugin.signal_warmup.minimum_bars
        if plugin_bars > required_bars:
            required_bars = plugin_bars
            driving_source = f"plugin:{plugin.plugin_id.value}"

    actual_bars = spec.warmup.signal_engine.minimum_bars
    if required_bars > 0 and actual_bars < required_bars:
        issues.append(
            make_error(
                ValidationIssueCode.WARMUP_BARS_BELOW_REQUIRED,
                path="warmup.signal_engine.minimum_bars",
                message=(
                    f"signal-engine minimum_bars {actual_bars} is below required "
                    f"{required_bars} (driven by {driving_source})"
                ),
                context=ExpectedActualVersionContext(
                    expected=str(required_bars),
                    actual=str(actual_bars),
                ),
            )
        )

    bar_tf = spec.warmup.signal_engine.bar_timeframe
    signal_tf = spec.timeframes.signal
    expected_tf = signal_tf
    if (
        plugin is not None
        and plugin.signal_warmup.timeframe_basis is WarmupTimeframeBasisV2.FIXED_TIMEFRAME
        and plugin.signal_warmup.fixed_timeframe is not None
    ):
        expected_tf = plugin.signal_warmup.fixed_timeframe

    if (
        _tf_minutes(bar_tf) != _tf_minutes(expected_tf)
        or bar_tf.unit is not expected_tf.unit
    ):
        issues.append(
            make_error(
                ValidationIssueCode.WARMUP_TIMEFRAME_MISMATCH,
                path="warmup.signal_engine.bar_timeframe",
                message="warmup bar_timeframe must match strategy signal timeframe",
                context=ExpectedActualVersionContext(
                    expected=_tf_label(expected_tf),
                    actual=_tf_label(bar_tf),
                ),
            )
        )

    if plugin is not None:
        issues.extend(_validate_plugin_padding(spec, plugin))
    return tuple(issues)


def _validate_plugin_padding(
    spec: StrategySpecV2,
    plugin: PluginDescriptorV2,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    source = plugin.source_loading_padding
    if source is not None:
        strategy_source = spec.warmup.source_loading
        if not _padding_covers(strategy_source.candle_history, source.candle_history) or (
            not _padding_covers(
                strategy_source.auxiliary_source_history,
                source.auxiliary_source_history,
            )
        ):
            issues.append(
                make_error(
                    ValidationIssueCode.WARMUP_SOURCE_PADDING_INSUFFICIENT,
                    path="warmup.source_loading",
                    message="source loading padding is insufficient for plugin contract",
                    context=ExpectedActualVersionContext(
                        expected="plugin_minimum",
                        actual="strategy_padding",
                    ),
                )
            )

    outcome = plugin.outcome_evaluation_padding
    if outcome is not None:
        strategy_outcome = spec.warmup.outcome_evaluation
        if not _padding_covers(
            strategy_outcome.post_window_duration,
            outcome.post_window_duration,
        ):
            issues.append(
                make_error(
                    ValidationIssueCode.WARMUP_OUTCOME_PADDING_INSUFFICIENT,
                    path="warmup.outcome_evaluation",
                    message=(
                        "outcome evaluation padding is insufficient for plugin contract"
                    ),
                    context=ExpectedActualVersionContext(
                        expected="plugin_minimum",
                        actual="strategy_padding",
                    ),
                )
            )
    return issues


def _expected_entry(
    spec: StrategySpecV2,
    plugin: PluginDescriptorV2 | None,
) -> tuple[AvailabilityTimingV2, EntryReferenceRuleV2]:
    if plugin is not None:
        return plugin.signal_decision_timing, plugin.entry_reference_rule
    return (
        AvailabilityTimingV2.SIGNAL_BAR_CLOSE,
        EntryReferenceRuleV2.SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR,
    )


def _validate_entry(
    spec: StrategySpecV2,
    plugin: PluginDescriptorV2 | None,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    expected_decision, expected_rule = _expected_entry(spec, plugin)
    if spec.entry.signal_decision_timing is not expected_decision:
        issues.append(
            make_error(
                ValidationIssueCode.ENTRY_DECISION_TIMING_MISMATCH,
                path="entry.signal_decision_timing",
                message="entry signal_decision_timing does not match expected contract",
                context=ExpectedActualVersionContext(
                    expected=expected_decision.value,
                    actual=spec.entry.signal_decision_timing.value,
                ),
            )
        )
    if spec.entry.entry_reference_rule is not expected_rule:
        issues.append(
            make_error(
                ValidationIssueCode.ENTRY_REFERENCE_RULE_MISMATCH,
                path="entry.entry_reference_rule",
                message="entry entry_reference_rule does not match expected contract",
                context=ExpectedActualVersionContext(
                    expected=expected_rule.value,
                    actual=spec.entry.entry_reference_rule.value,
                ),
            )
        )
    return tuple(issues)


def _validate_exit(spec: StrategySpecV2) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if spec.exit.take_profit.value <= 0:
        issues.append(
            make_error(
                ValidationIssueCode.EXIT_RATE_NON_POSITIVE,
                path="exit.take_profit",
                message="take_profit must be > 0",
                context=None,
            )
        )
    if spec.exit.stop_loss.value <= 0:
        issues.append(
            make_error(
                ValidationIssueCode.EXIT_RATE_NON_POSITIVE,
                path="exit.stop_loss",
                message="stop_loss must be > 0",
                context=None,
            )
        )
    if spec.exit.horizon.value <= 0:
        issues.append(
            make_error(
                ValidationIssueCode.EXIT_HORIZON_NON_POSITIVE,
                path="exit.horizon",
                message="horizon must be > 0",
                context=None,
            )
        )
    return tuple(issues)


def _validate_execution_costs_portfolio(
    spec: StrategySpecV2,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if spec.costs.roundtrip_cost.value < 0:
        issues.append(
            make_error(
                ValidationIssueCode.COST_ROUNDTRIP_NEGATIVE,
                path="costs.roundtrip_cost",
                message="roundtrip_cost must be >= 0",
                context=None,
            )
        )
    if spec.costs.slippage is ModelingStatus.MODELED:
        issues.append(
            make_error(
                ValidationIssueCode.COST_SLIPPAGE_MODELED_UNSUPPORTED,
                path="costs.slippage",
                message="phase-1 does not support modeled slippage",
                context=None,
            )
        )
    if spec.costs.funding is ModelingStatus.MODELED:
        issues.append(
            make_error(
                ValidationIssueCode.COST_FUNDING_MODELED_UNSUPPORTED,
                path="costs.funding",
                message="phase-1 does not support modeled funding",
                context=None,
            )
        )
    if spec.execution_assumptions.rounding_status is ModelingStatus.MODELED:
        issues.append(
            make_error(
                ValidationIssueCode.EXECUTION_ROUNDING_MODELED_UNSUPPORTED,
                path="execution_assumptions.rounding_status",
                message="phase-1 does not support modeled rounding",
                context=None,
            )
        )
    if spec.portfolio_assumptions.compounding is True:
        issues.append(
            make_error(
                ValidationIssueCode.PORTFOLIO_COMPOUNDING_UNSUPPORTED,
                path="portfolio_assumptions.compounding",
                message="phase-1 requires compounding=false",
                context=None,
            )
        )
    return tuple(issues)


def _target_key(target: object) -> tuple[object, ...]:
    if isinstance(target, SignalTimeframeTargetV2):
        return ("signal_timeframe",)
    if isinstance(target, RoundtripCostTargetV2):
        return ("roundtrip_cost",)
    if isinstance(target, FeatureParameterTargetV2):
        return (
            "feature_parameter",
            target.feature_alias.value,
            target.parameter_name.value,
        )
    if isinstance(target, PluginConfigParameterTargetV2):
        return ("plugin_config_parameter", target.parameter_name.value)
    if isinstance(target, ExitParameterTargetV2):
        return ("exit_parameter", target.parameter_name.value)
    return ("unknown",)


def _validate_research_space(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
    index: FeatureResolutionIndex,
    plugin: PluginDescriptorV2 | None,
    p4a_codes: set[ValidationIssueCode],
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    dimensions = spec.research_parameter_space.dimensions
    seen_ids: set[str] = set()
    seen_targets: set[tuple[object, ...]] = set()

    for i, dimension in enumerate(dimensions):
        path = f"research_parameter_space.dimensions[{i}]"
        dim_id = dimension.dimension_id.value
        if dim_id in seen_ids:
            issues.append(
                make_error(
                    ValidationIssueCode.RESEARCH_DUPLICATE_DIMENSION_ID,
                    path=f"{path}.dimension_id",
                    message=f"duplicate research dimension_id {dim_id!r}",
                    context=UnknownIdentifierContext(
                        identifier=dimension.dimension_id
                    ),
                )
            )
        seen_ids.add(dim_id)

        key = _target_key(dimension.target)
        if key in seen_targets:
            issues.append(
                make_error(
                    ValidationIssueCode.RESEARCH_DUPLICATE_TARGET,
                    path=f"{path}.target",
                    message="duplicate research target",
                    context=None,
                )
            )
        seen_targets.add(key)

        if not dimension.candidates:
            issues.append(
                make_error(
                    ValidationIssueCode.RESEARCH_CANDIDATES_EMPTY,
                    path=f"{path}.candidates",
                    message="research dimension candidates must not be empty",
                    context=None,
                )
            )
            continue

        issues.extend(
            _validate_research_dimension_candidates(
                spec=spec,
                catalogs=catalogs,
                index=index,
                plugin=plugin,
                p4a_codes=p4a_codes,
                dimension_index=i,
                dimension=dimension,
            )
        )
    return tuple(issues)


def _validate_research_dimension_candidates(
    *,
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
    index: FeatureResolutionIndex,
    plugin: PluginDescriptorV2 | None,
    p4a_codes: set[ValidationIssueCode],
    dimension_index: int,
    dimension: object,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    path = f"research_parameter_space.dimensions[{dimension_index}]"
    target = dimension.target

    if isinstance(target, FeatureParameterTargetV2):
        if not index.has_alias(target.feature_alias):
            issues.append(
                make_error(
                    ValidationIssueCode.RESEARCH_UNKNOWN_FEATURE_ALIAS,
                    path=f"{path}.target.feature_alias",
                    message=f"unknown feature alias {target.feature_alias.value!r}",
                    context=UnknownIdentifierContext(identifier=target.feature_alias),
                )
            )
            return issues
        entry = index.get_binding_and_feature(target.feature_alias)
        if entry is None:
            return issues
        _binding, feature = entry
        definition = _find_param_def(feature.parameters, target.parameter_name)
        if definition is None:
            issues.append(
                make_error(
                    ValidationIssueCode.RESEARCH_UNKNOWN_FEATURE_PARAMETER,
                    path=f"{path}.target.parameter_name",
                    message=f"unknown feature parameter {target.parameter_name.value!r}",
                    context=ParameterNameContext(parameter_name=target.parameter_name),
                )
            )
            return issues
        for j, candidate in enumerate(dimension.candidates):
            raw = validate_parameter_value(
                path=f"{path}.candidates[{j}]",
                definition=definition,
                value=candidate,
                code_prefix="FEATURE",
            )
            issues.extend(remap_parameter_issues_to_research(raw))
        return issues

    if isinstance(target, PluginConfigParameterTargetV2):
        if not isinstance(spec.signal, PluginSignalSpec):
            issues.append(
                make_error(
                    ValidationIssueCode.RESEARCH_PLUGIN_TARGET_WITHOUT_PLUGIN,
                    path=f"{path}.target",
                    message="plugin config research target requires PluginSignalSpec",
                    context=None,
                )
            )
            return issues
        if plugin is None:
            return issues
        definitions = config_parameter_definitions(plugin.parameters)
        definition = _find_param_def(definitions, target.parameter_name)
        if definition is None:
            issues.append(
                make_error(
                    ValidationIssueCode.RESEARCH_UNKNOWN_PLUGIN_PARAMETER,
                    path=f"{path}.target.parameter_name",
                    message=f"unknown plugin parameter {target.parameter_name.value!r}",
                    context=ParameterNameContext(parameter_name=target.parameter_name),
                )
            )
            return issues
        for j, candidate in enumerate(dimension.candidates):
            raw = validate_parameter_value(
                path=f"{path}.candidates[{j}]",
                definition=definition,
                value=candidate,
                code_prefix="PLUGIN",
            )
            issues.extend(remap_parameter_issues_to_research(raw))
        return issues

    if isinstance(target, ExitParameterTargetV2):
        name = target.parameter_name.value
        if name not in {"take_profit", "stop_loss", "horizon"}:
            issues.append(
                make_error(
                    ValidationIssueCode.RESEARCH_UNKNOWN_EXIT_PARAMETER,
                    path=f"{path}.target.parameter_name",
                    message=f"unknown exit parameter {name!r}",
                    context=ParameterNameContext(parameter_name=target.parameter_name),
                )
            )
            return issues
        for j, candidate in enumerate(dimension.candidates):
            cand_path = f"{path}.candidates[{j}]"
            if name in {"take_profit", "stop_loss"}:
                issues.extend(
                    _check_rate_candidate(
                        candidate=candidate,
                        path=cand_path,
                        baseline_unit=(
                            spec.exit.take_profit.unit
                            if name == "take_profit"
                            else spec.exit.stop_loss.unit
                        ),
                        require_positive=True,
                    )
                )
            else:
                issues.extend(
                    _check_duration_candidate(
                        candidate=candidate,
                        path=cand_path,
                        baseline=spec.exit.horizon,
                    )
                )
        return issues

    if isinstance(target, RoundtripCostTargetV2):
        for j, candidate in enumerate(dimension.candidates):
            issues.extend(
                _check_rate_candidate(
                    candidate=candidate,
                    path=f"{path}.candidates[{j}]",
                    baseline_unit=spec.costs.roundtrip_cost.unit,
                    require_positive=False,
                )
            )
        return issues

    if isinstance(target, SignalTimeframeTargetV2):
        for j, candidate in enumerate(dimension.candidates):
            cand_path = f"{path}.candidates[{j}]"
            if not isinstance(candidate, TimeframeParam):
                actual = _safe_param_type(candidate)
                issues.append(
                    make_error(
                        ValidationIssueCode.RESEARCH_CANDIDATE_TYPE,
                        path=cand_path,
                        message="signal timeframe candidate must be TimeframeParam",
                        context=ExpectedActualTypeContext(
                            expected=ParameterValueType.TIMEFRAME,
                            actual=actual,
                        ),
                    )
                )
                continue
            tf = candidate.value
            if plugin is not None:
                minutes = _tf_minutes(tf)
                contract = plugin.signal_timeframe
                ok = False
                if minutes is not None and tf.unit is TimeframeUnit.MINUTES:
                    if contract.mode is SignalTimeframeModeV2.FIXED:
                        ok = minutes == contract.reference_minutes
                    elif contract.mode is SignalTimeframeModeV2.ALLOWED_SET:
                        ok = minutes in contract.allowed_minutes
                    else:
                        ok = True
                if not ok:
                    issues.append(
                        make_error(
                            ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT,
                            path=cand_path,
                            message="signal timeframe candidate is not allowed by plugin",
                            context=ExpectedActualVersionContext(
                                expected=str(contract.allowed_minutes),
                                actual=_tf_label(tf),
                            ),
                        )
                    )
        return issues

    return issues


def _find_param_def(
    definitions: tuple[ParameterDefinitionV2, ...],
    name: StableIdentifier,
) -> ParameterDefinitionV2 | None:
    for definition in definitions:
        if definition.name.value == name.value:
            return definition
    return None


def _safe_param_type(value: ParamValue) -> ParameterValueType:
    try:
        return param_value_to_parameter_type(value)
    except TypeError:
        return ParameterValueType.STRING


def _check_rate_candidate(
    *,
    candidate: ParamValue,
    path: str,
    baseline_unit: RateUnit,
    require_positive: bool,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not isinstance(candidate, RateParam):
        issues.append(
            make_error(
                ValidationIssueCode.RESEARCH_CANDIDATE_TYPE,
                path=path,
                message="candidate must be RateParam",
                context=ExpectedActualTypeContext(
                    expected=ParameterValueType.RATE,
                    actual=_safe_param_type(candidate),
                ),
            )
        )
        return issues
    rate = candidate.value
    if rate.unit is not baseline_unit:
        issues.append(
            make_error(
                ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT,
                path=path,
                message=f"rate unit must be {baseline_unit.value}",
                context=ExpectedActualVersionContext(
                    expected=baseline_unit.value,
                    actual=rate.unit.value,
                ),
            )
        )
    if require_positive and rate.value <= 0:
        issues.append(
            make_error(
                ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT,
                path=path,
                message="rate candidate must be > 0",
                context=None,
            )
        )
    if not require_positive and rate.value < 0:
        issues.append(
            make_error(
                ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT,
                path=path,
                message="rate candidate must be >= 0",
                context=None,
            )
        )
    return issues


def _check_duration_candidate(
    *,
    candidate: ParamValue,
    path: str,
    baseline: DurationValue,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not isinstance(candidate, DurationParam):
        issues.append(
            make_error(
                ValidationIssueCode.RESEARCH_CANDIDATE_TYPE,
                path=path,
                message="candidate must be DurationParam",
                context=ExpectedActualTypeContext(
                    expected=ParameterValueType.DURATION,
                    actual=_safe_param_type(candidate),
                ),
            )
        )
        return issues
    duration = candidate.value
    if duration.value <= 0:
        issues.append(
            make_error(
                ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT,
                path=path,
                message="duration candidate must be > 0",
                context=None,
            )
        )
    if duration.unit is baseline.unit:
        return issues
    if (
        duration.unit in {DurationUnit.MINUTES, DurationUnit.HOURS}
        and baseline.unit in {DurationUnit.MINUTES, DurationUnit.HOURS}
    ):
        return issues
    issues.append(
        make_error(
            ValidationIssueCode.RESEARCH_CANDIDATE_CONSTRAINT,
            path=path,
            message="duration unit is not convertible to baseline unit",
            context=ExpectedActualVersionContext(
                expected=baseline.unit.value,
                actual=duration.unit.value,
            ),
        )
    )
    return issues


def _validate_provenance(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
    plugin: PluginDescriptorV2 | None,
    p4a_codes: set[ValidationIssueCode],
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    refs = spec.provenance.plugin_refs
    expected_catalog = CATALOG_CONTRACT_VERSION

    if spec.provenance.catalog_contract_version.value != expected_catalog:
        issues.append(
            make_error(
                ValidationIssueCode.PROVENANCE_CATALOG_VERSION_MISMATCH,
                path="provenance.catalog_contract_version",
                message=f"catalog_contract_version must be {expected_catalog!r}",
                context=ExpectedActualVersionContext(
                    expected=expected_catalog,
                    actual=spec.provenance.catalog_contract_version.value,
                ),
            )
        )
    else:
        mismatched_binding = next(
            (
                binding.catalog_contract_version.value
                for binding in spec.features
                if binding.catalog_contract_version.value != expected_catalog
            ),
            None,
        )
        if mismatched_binding is not None:
            issues.append(
                make_error(
                    ValidationIssueCode.PROVENANCE_CATALOG_VERSION_MISMATCH,
                    path="provenance.catalog_contract_version",
                    message="catalog_contract_version does not match feature bindings",
                    context=ExpectedActualVersionContext(
                        expected=mismatched_binding,
                        actual=expected_catalog,
                    ),
                )
            )
        elif (
            isinstance(spec.signal, PluginSignalSpec)
            and spec.signal.plugin.contract_version.value != expected_catalog
        ):
            issues.append(
                make_error(
                    ValidationIssueCode.PROVENANCE_CATALOG_VERSION_MISMATCH,
                    path="provenance.catalog_contract_version",
                    message="catalog_contract_version does not match plugin contract",
                    context=ExpectedActualVersionContext(
                        expected=spec.signal.plugin.contract_version.value,
                        actual=expected_catalog,
                    ),
                )
            )

    seen_plugin_ids: set[str] = set()
    for i, ref in enumerate(refs):
        pid = ref.plugin_id.value
        if pid in seen_plugin_ids:
            issues.append(
                make_error(
                    ValidationIssueCode.PROVENANCE_PLUGIN_REF_DUPLICATE,
                    path=f"provenance.plugin_refs[{i}]",
                    message=f"duplicate provenance plugin_id {pid!r}",
                    context=UnknownIdentifierContext(identifier=ref.plugin_id),
                )
            )
        seen_plugin_ids.add(pid)
        try:
            descriptor = catalogs.plugins.get(pid)
        except Exception:
            issues.append(
                make_error(
                    ValidationIssueCode.PROVENANCE_PLUGIN_REF_UNKNOWN,
                    path=f"provenance.plugin_refs[{i}].plugin_id",
                    message=f"unknown provenance plugin_id {pid!r}",
                    context=UnknownIdentifierContext(identifier=ref.plugin_id),
                )
            )
            continue
        if ref.contract_version.value != descriptor.contract_version.value:
            issues.append(
                make_error(
                    ValidationIssueCode.PROVENANCE_PLUGIN_REF_VERSION,
                    path=f"provenance.plugin_refs[{i}].contract_version",
                    message="provenance plugin contract_version mismatch",
                    context=ExpectedActualVersionContext(
                        expected=descriptor.contract_version.value,
                        actual=ref.contract_version.value,
                    ),
                )
            )

    if isinstance(spec.signal, PluginSignalSpec):
        expected_id = spec.signal.plugin.plugin_id.value
        expected_version = spec.signal.plugin.contract_version.value
        has_exact = any(
            ref.plugin_id.value == expected_id
            and ref.contract_version.value == expected_version
            for ref in refs
        )
        if (
            not has_exact
            and expected_id not in seen_plugin_ids
            and ValidationIssueCode.PLUGIN_UNKNOWN not in p4a_codes
        ):
            issues.append(
                make_error(
                    ValidationIssueCode.PROVENANCE_PLUGIN_REFS_MISSING,
                    path="provenance.plugin_refs",
                    message=(
                        f"provenance.plugin_refs must include signal plugin "
                        f"{expected_id!r}"
                    ),
                    context=UnknownIdentifierContext(
                        identifier=spec.signal.plugin.plugin_id
                    ),
                )
            )
        unexpected_other_ids = any(
            ref.plugin_id.value != expected_id for ref in refs
        )
        if unexpected_other_ids:
            issues.append(
                make_error(
                    ValidationIssueCode.PROVENANCE_PLUGIN_REFS_UNEXPECTED,
                    path="provenance.plugin_refs",
                    message=(
                        "provenance.plugin_refs contains unexpected plugin references"
                    ),
                    context=None,
                )
            )

        if plugin is not None:
            if spec.provenance.causality_status is not plugin.causality_status:
                issues.append(
                    make_error(
                        ValidationIssueCode.PROVENANCE_CAUSALITY_MISMATCH,
                        path="provenance.causality_status",
                        message="provenance causality_status must match plugin",
                        context=ExpectedActualVersionContext(
                            expected=plugin.causality_status.value,
                            actual=spec.provenance.causality_status.value,
                        ),
                    )
                )
    elif refs:
        issues.append(
            make_error(
                ValidationIssueCode.PROVENANCE_PLUGIN_REFS_UNEXPECTED,
                path="provenance.plugin_refs",
                message="non-plugin signals require empty provenance.plugin_refs",
                context=None,
            )
        )

    allowed = spec.validation_requirements.allowed_causality_statuses
    if allowed and spec.provenance.causality_status not in allowed:
        issues.append(
            make_error(
                ValidationIssueCode.PROVENANCE_CAUSALITY_NOT_ALLOWED,
                path="provenance.causality_status",
                message="provenance causality_status is not in allowed_causality_statuses",
                context=None,
            )
        )
    return tuple(issues)
