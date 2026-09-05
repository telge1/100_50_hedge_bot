"""Catalog/v2 feature, operator, and plugin descriptor tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from orderbook_analyse.strategy_lab.catalogs.v2 import (
    CATALOG_CONTRACT_VERSION,
    FEATURE_CATALOG_V2,
    OPERATOR_CATALOG_V2,
    PLUGIN_CATALOG_V2,
    assert_production_catalog_integrity_v2,
    get_feature_v2,
    get_operator_v2,
    get_plugin_v2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2 import (
    CollectionShape,
    EvaluationSemanticsV2,
    OperandOriginV2,
    OperandTypeConstraintV2,
    SelectedSignalTimeframeGranularityV2,
    SignalTimeframeModeV2,
    SnapshotGranularityV2,
    TimeframeGranularityV2,
    WarmupTimeframeBasisV2,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier


def test_catalog_v2_contract_version() -> None:
    assert CATALOG_CONTRACT_VERSION == "catalog/v2"


def test_feature_catalog_three_outputs() -> None:
    assert len(FEATURE_CATALOG_V2) == 3
    ema = get_feature_v2("ema")
    atr = get_feature_v2("atr_wilder")
    clusters = get_feature_v2("lld_liquidity_clusters")
    assert len(ema.outputs) == 1
    assert ema.outputs[0].output_id.value == "value"
    assert len(atr.outputs) == 1
    assert len(clusters.outputs) == 1
    assert clusters.outputs[0].output_id.value == "snapshots"


def test_feature_output_ids_unique() -> None:
    for feature in FEATURE_CATALOG_V2:
        ids = [output.output_id.value for output in feature.outputs]
        assert len(ids) == len(set(ids))


def test_cluster_snapshots_collection_shape_is_sequence() -> None:
    clusters = get_feature_v2("lld_liquidity_clusters")
    output = clusters.outputs[0]
    assert output.collection_shape is CollectionShape.SEQUENCE
    assert "list" in output.warmup.notes.lower() or "sequence" in output.description.lower()


def test_operator_catalog_excludes_logical_ops() -> None:
    ids = set(OPERATOR_CATALOG_V2.ids)
    assert "and" not in ids
    assert "or" not in ids
    assert "not" not in ids
    assert ids == {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "crosses_above",
        "crosses_below",
    }


def test_gt_series_vs_decimal_param_signature_exists() -> None:
    gt = get_operator_v2("gt")
    assert any(
        sig.operands[0].type_constraint is OperandTypeConstraintV2.DECIMAL_SERIES
        and sig.operands[1].type_constraint is OperandTypeConstraintV2.DECIMAL_LITERAL
        for sig in gt.signatures
    )


def test_gt_series_vs_int_param_signature_exists() -> None:
    gt = get_operator_v2("gt")
    assert any(
        sig.operands[1].type_constraint is OperandTypeConstraintV2.INTEGER_LITERAL
        for sig in gt.signatures
    )


def test_cross_operator_observation_contract() -> None:
    cross = get_operator_v2("crosses_above")
    sig = cross.signatures[0]
    assert sig.evaluation_semantics is EvaluationSemanticsV2.CROSS_REQUIRES_PRIOR_AND_CURRENT
    assert sig.observation is not None
    assert sig.observation.requires_previous_observation is True
    assert all(
        op.origin is OperandOriginV2.FEATURE_OUTPUT
        and op.type_constraint is OperandTypeConstraintV2.DECIMAL_SERIES
        for op in sig.operands
    )


def test_rate_param_not_in_numeric_compare_signatures() -> None:
    gt = get_operator_v2("gt")
    for sig in gt.signatures:
        for op in sig.operands:
            assert op.type_constraint != OperandTypeConstraintV2.DECIMAL_LITERAL or (
                op.origin is OperandOriginV2.LITERAL_PARAM
            )


def test_plugin_descriptors_complete() -> None:
    assert len(PLUGIN_CATALOG_V2) == 3
    edc = get_plugin_v2("edc_m0_strict_sync")
    cluster = get_plugin_v2("cluster_sweep")
    ezm = get_plugin_v2("ema_zone_microstructure_confirmation")
    assert edc.contract_version.value == "catalog/v2"
    assert cluster.contract_version.value == "catalog/v2"
    assert ezm.contract_version.value == "catalog/v2"
    assert edc.confirmation_policy is not None
    assert cluster.confirmation_policy is None


def test_edc_mode_contract_required_m0_strict_sync() -> None:
    from orderbook_analyse.strategy_lab.models.contracts_v2 import (
        PluginModeRequirementV2,
    )

    edc = get_plugin_v2("edc_m0_strict_sync")
    assert edc.mode_contract.requirement is PluginModeRequirementV2.REQUIRED
    assert tuple(mode.value for mode in edc.mode_contract.allowed_modes) == (
        "m0_strict_sync",
    )


def test_cluster_mode_contract_not_applicable() -> None:
    from orderbook_analyse.strategy_lab.models.contracts_v2 import (
        PluginModeRequirementV2,
        PluginParameterBindingTargetV2,
    )

    cluster = get_plugin_v2("cluster_sweep")
    assert cluster.mode_contract.requirement is PluginModeRequirementV2.NOT_APPLICABLE
    assert cluster.mode_contract.allowed_modes == ()
    assert cluster.parameters
    assert all(
        item.binding_target is PluginParameterBindingTargetV2.PLUGIN_REF_CONFIG
        for item in cluster.parameters
    )


def test_cluster_warmup_selected_signal_timeframe() -> None:
    cluster = get_plugin_v2("cluster_sweep")
    warmup = cluster.signal_warmup
    assert warmup.minimum_bars == 79
    assert warmup.timeframe_basis is WarmupTimeframeBasisV2.SELECTED_SIGNAL_TIMEFRAME
    assert warmup.fixed_timeframe is None


def test_cluster_signal_tf_granularity_uses_selected_binding() -> None:
    cluster = get_plugin_v2("cluster_sweep")
    assert cluster.signal_timeframe.mode is SignalTimeframeModeV2.ALLOWED_SET
    assert set(cluster.signal_timeframe.allowed_minutes) == {5, 15}
    signal_reqs = [
        req
        for req in cluster.data_requirements
        if req.requirement_id.value
        in {"cluster_candles_signal_tf", "cluster_liquidity_locations"}
    ]
    assert len(signal_reqs) == 2
    for req in signal_reqs:
        assert isinstance(req.granularity, SelectedSignalTimeframeGranularityV2)
        assert req.granularity.binds_to_selected_signal_timeframe is True


def test_cluster_no_hidden_reference_15m_on_signal_requirements() -> None:
    cluster = get_plugin_v2("cluster_sweep")
    ref_minutes = cluster.signal_timeframe.reference_minutes
    for req in cluster.data_requirements:
        if req.role.value.endswith("required") and req.required:
            gran = req.granularity
            if isinstance(gran, TimeframeGranularityV2):
                if req.requirement_id.value == "cluster_candles_execution_1m":
                    assert gran.timeframe.value == 1
                else:
                    assert gran.timeframe.value != ref_minutes or ref_minutes == 1
            assert not isinstance(gran, SnapshotGranularityV2)


def test_edc_signal_candles_fixed_5m() -> None:
    edc = get_plugin_v2("edc_m0_strict_sync")
    assert edc.signal_timeframe.mode is SignalTimeframeModeV2.FIXED
    candles = next(
        req
        for req in edc.data_requirements
        if req.requirement_id.value == "edc_candles_signal_tf"
    )
    assert isinstance(candles.granularity, TimeframeGranularityV2)
    assert candles.granularity.timeframe.value == 5


def test_cluster_optional_enrichment_1m_aggregates() -> None:
    cluster = get_plugin_v2("cluster_sweep")
    for req_id in (
        "cluster_public_trades_1m",
        "cluster_orderbook_ob200_v3_1m",
        "cluster_open_interest_1m",
    ):
        req = next(
            r for r in cluster.data_requirements if r.requirement_id.value == req_id
        )
        assert isinstance(req.granularity, TimeframeGranularityV2)
        assert req.granularity.timeframe.value == 1


def test_cluster_liquidations_native_event_stream() -> None:
    cluster = get_plugin_v2("cluster_sweep")
    liq = next(
        req
        for req in cluster.data_requirements
        if req.requirement_id.value == "cluster_liquidations"
    )
    from orderbook_analyse.strategy_lab.models.contracts_v2 import (
        EventStreamGranularityV2,
    )

    assert isinstance(liq.granularity, EventStreamGranularityV2)


def test_edc_liquidations_event_stream_granularity() -> None:
    edc = get_plugin_v2("edc_m0_strict_sync")
    liq = next(
        req
        for req in edc.data_requirements
        if req.requirement_id.value == "edc_liquidations"
    )
    from orderbook_analyse.strategy_lab.models.contracts_v2 import (
        EventStreamGranularityV2,
    )

    assert isinstance(liq.granularity, EventStreamGranularityV2)


def test_catalog_integrity_v2_passes() -> None:
    report = assert_production_catalog_integrity_v2()
    assert report.ok


def test_reserved_plugin_config_keys_typed() -> None:
    from orderbook_analyse.strategy_lab.models.contracts_v2 import (
        RESERVED_PLUGIN_CONFIG_KEYS,
    )

    for key in RESERVED_PLUGIN_CONFIG_KEYS:
        assert isinstance(key, StableIdentifier)
