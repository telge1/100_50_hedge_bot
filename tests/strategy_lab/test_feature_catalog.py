"""Feature catalog tests."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from orderbook_analyse.strategy_lab.catalogs import (
    FEATURE_CATALOG,
    BoundFeatureRequirement,
    BoundParameterBinding,
    CatalogRegistry,
    UnknownCatalogEntryError,
    get_feature,
    validate_catalog_integrity,
)
from orderbook_analyse.strategy_lab.catalogs.features import EMA
from orderbook_analyse.strategy_lab.catalogs.models import (
    AvailabilityTiming,
    DataSourceKind,
    MissingValuePolicy,
    ValueType,
)
from orderbook_analyse.strategy_lab.models.enums import RateUnit
from orderbook_analyse.strategy_lab.models.strategy import (
    DecimalParam,
    IntParam,
    RateParam,
    RateValue,
)


def test_generic_features_present() -> None:
    assert set(FEATURE_CATALOG.ids) == {
        "atr_wilder",
        "ema",
        "lld_liquidity_clusters",
    }
    assert "ema_triple_9_20_59" not in FEATURE_CATALOG.ids
    assert "atr_wilder_14" not in FEATURE_CATALOG.ids


def test_ema_requires_explicit_period_without_default() -> None:
    ema = get_feature("ema")
    period = ema.parameters[0]
    assert period.name == "period"
    assert period.required is True
    assert period.must_be_explicit is True
    assert period.legacy_reference_value is None


def test_atr_wilder_requires_explicit_period() -> None:
    atr = get_feature("atr_wilder")
    period = atr.parameters[0]
    assert period.name == "period"
    assert period.required is True
    assert period.must_be_explicit is True


def test_bound_ema_usages_for_reference_periods() -> None:
    fast = BoundFeatureRequirement(
        alias="ema_fast",
        feature_id="ema",
        bindings=(BoundParameterBinding(name="period", value=IntParam(value=9)),),
    )
    medium = BoundFeatureRequirement(
        alias="ema_medium",
        feature_id="ema",
        bindings=(BoundParameterBinding(name="period", value=IntParam(value=20)),),
    )
    slow = BoundFeatureRequirement(
        alias="ema_slow",
        feature_id="ema",
        bindings=(BoundParameterBinding(name="period", value=IntParam(value=59)),),
    )
    assert fast.bindings[0].value.value == 9
    assert medium.bindings[0].value.value == 20
    assert slow.bindings[0].value.value == 59


def test_ema10_50_without_new_feature_descriptor() -> None:
    ema10 = BoundFeatureRequirement(
        alias="ema_fast",
        feature_id="ema",
        bindings=(BoundParameterBinding(name="period", value=IntParam(value=10)),),
    )
    ema50 = BoundFeatureRequirement(
        alias="ema_slow",
        feature_id="ema",
        bindings=(BoundParameterBinding(name="period", value=IntParam(value=50)),),
    )
    assert ema10.feature_id == "ema"
    assert ema50.feature_id == "ema"


def test_integrity_rejects_period_zero_binding() -> None:
    bad = dataclasses.replace(
        EMA,
        feature_id="ema",
    )
    bad_registry = CatalogRegistry(
        name="feature",
        entries=(bad,),
        id_getter=lambda d: d.feature_id,
    )
    bad_plugin_binding = BoundFeatureRequirement(
        alias="ema_fast",
        feature_id="ema",
        bindings=(BoundParameterBinding(name="period", value=IntParam(value=0)),),
    )
    from orderbook_analyse.strategy_lab.catalogs import PLUGIN_CATALOG

    bad_plugin = dataclasses.replace(
        PLUGIN_CATALOG.get("cluster_sweep"),
        required_features=(bad_plugin_binding,),
    )
    bad_plugin_registry = CatalogRegistry(
        name="plugin",
        entries=(bad_plugin,),
        id_getter=lambda d: d.plugin_id,
    )
    report = validate_catalog_integrity(
        features=bad_registry,
        plugins=bad_plugin_registry,
    )
    assert not report.ok
    assert any(issue.code == "INVALID_BOUND_PARAMETER_VALUE" for issue in report.issues)


def test_bool_not_accepted_as_period_binding() -> None:
    with pytest.raises(TypeError):
        IntParam(value=True)  # type: ignore[arg-type]


def test_feature_lookup_and_unknown_id() -> None:
    feature = get_feature("ema")
    assert feature.output_type is ValueType.PRICE_SERIES
    with pytest.raises(UnknownCatalogEntryError):
        FEATURE_CATALOG.get("ema_triple_9_20_59")


def test_lld_feature_contract() -> None:
    lld = get_feature("lld_liquidity_clusters")
    assert DataSourceKind.LIQUIDITY_LOCATIONS in lld.data_requirements
    assert lld.availability is AvailabilityTiming.PRIOR_BAR_OPEN
    assert lld.missing_value_policy is MissingValuePolicy.RETURN_UNAVAILABLE


def test_feature_provenance_present() -> None:
    for feature in FEATURE_CATALOG:
        assert feature.provenance
        assert feature.provenance[0].module
        assert feature.provenance[0].path


def test_lld_gap_pct_uses_rate_percent_not_ambiguous_decimal() -> None:
    lld = get_feature("lld_liquidity_clusters")
    gap = next(param for param in lld.parameters if param.name == "gap_pct")
    assert gap.value_type is ValueType.RATE
    assert gap.required_rate_unit is RateUnit.PERCENT
    assert gap.legacy_reference_value == "0.10"


def test_cluster_gap_pct_binding_is_rate_percent_canonical_value() -> None:
    from orderbook_analyse.strategy_lab.catalogs import get_plugin

    plugin = get_plugin("cluster_sweep")
    clusters = next(req for req in plugin.required_features if req.alias == "clusters")
    gap_binding = next(binding for binding in clusters.bindings if binding.name == "gap_pct")
    assert isinstance(gap_binding.value, RateParam)
    assert gap_binding.value.value.value == Decimal("0.10")
    assert gap_binding.value.value.unit is RateUnit.PERCENT
    assert not isinstance(gap_binding.value, DecimalParam)


def _cluster_sweep_with_clusters_binding(
    clusters: BoundFeatureRequirement,
):
    from orderbook_analyse.strategy_lab.catalogs import PLUGIN_CATALOG

    original = PLUGIN_CATALOG.get("cluster_sweep")
    rebuilt_features = tuple(
        clusters if req.alias == "clusters" else req for req in original.required_features
    )
    return dataclasses.replace(original, required_features=rebuilt_features)


def test_integrity_rejects_wrong_rate_unit_for_gap_pct() -> None:
    from orderbook_analyse.strategy_lab.catalogs import PLUGIN_CATALOG

    clusters = next(
        req
        for req in PLUGIN_CATALOG.get("cluster_sweep").required_features
        if req.alias == "clusters"
    )
    bad_clusters = dataclasses.replace(
        clusters,
        bindings=(
            BoundParameterBinding(
                name="gap_pct",
                value=RateParam(
                    value=RateValue(value=Decimal("0.10"), unit=RateUnit.FRACTION)
                ),
            ),
            next(binding for binding in clusters.bindings if binding.name == "minimum_pools"),
        ),
    )
    bad_plugin = _cluster_sweep_with_clusters_binding(bad_clusters)
    bad_registry = CatalogRegistry(
        name="plugin",
        entries=(bad_plugin,),
        id_getter=lambda d: d.plugin_id,
    )
    report = validate_catalog_integrity(plugins=bad_registry)
    assert not report.ok
    assert any(issue.code == "INVALID_RATE_UNIT" for issue in report.issues)


def test_integrity_rejects_decimal_param_for_rate_feature() -> None:
    from orderbook_analyse.strategy_lab.catalogs import PLUGIN_CATALOG

    clusters = next(
        req
        for req in PLUGIN_CATALOG.get("cluster_sweep").required_features
        if req.alias == "clusters"
    )
    bad_clusters = dataclasses.replace(
        clusters,
        bindings=(
            BoundParameterBinding(
                name="gap_pct",
                value=DecimalParam(value=Decimal("0.10")),
            ),
            next(binding for binding in clusters.bindings if binding.name == "minimum_pools"),
        ),
    )
    bad_plugin = _cluster_sweep_with_clusters_binding(bad_clusters)
    bad_registry = CatalogRegistry(
        name="plugin",
        entries=(bad_plugin,),
        id_getter=lambda d: d.plugin_id,
    )
    report = validate_catalog_integrity(plugins=bad_registry)
    assert not report.ok
    assert any(issue.code == "BOUND_PARAMETER_TYPE_MISMATCH" for issue in report.issues)
