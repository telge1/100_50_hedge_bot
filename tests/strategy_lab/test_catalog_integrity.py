"""Catalog registry and integrity tests."""

from __future__ import annotations

import dataclasses

import pytest

from orderbook_analyse.strategy_lab.catalogs import (
    FEATURE_CATALOG,
    PLUGIN_CATALOG,
    BoundFeatureRequirement,
    BoundParameterBinding,
    CatalogRegistry,
    InvalidCatalogDefinitionError,
    UnknownCatalogEntryError,
    assert_production_catalog_integrity,
    build_feature_catalog,
    validate_catalog_integrity,
)
from orderbook_analyse.strategy_lab.catalogs.features import EMA
from orderbook_analyse.strategy_lab.models.strategy import IntParam


def test_registry_deterministic_order_and_ids() -> None:
    first = tuple(FEATURE_CATALOG.ids)
    second = tuple(build_feature_catalog().ids)
    assert first == second
    assert first == tuple(sorted(first))


def test_registry_lookup_and_unknown() -> None:
    feature = FEATURE_CATALOG.get("ema")
    assert feature.feature_id == "ema"
    with pytest.raises(UnknownCatalogEntryError):
        FEATURE_CATALOG.get("does_not_exist")


def test_registry_rejects_duplicate_ids() -> None:
    duplicate = dataclasses.replace(
        EMA,
        description="duplicate for test",
    )
    with pytest.raises(InvalidCatalogDefinitionError):
        CatalogRegistry(
            name="feature",
            entries=(EMA, duplicate),
            id_getter=lambda d: d.feature_id,
        )


def test_production_catalog_integrity_passes() -> None:
    report = assert_production_catalog_integrity()
    assert report.ok


def test_integrity_rejects_unknown_feature_reference() -> None:
    bad_plugin = dataclasses.replace(
        PLUGIN_CATALOG.get("cluster_sweep"),
        required_features=(
            BoundFeatureRequirement(
                alias="missing",
                feature_id="missing_feature",
                bindings=(
                    BoundParameterBinding(
                        name="period",
                        value=IntParam(value=9),
                    ),
                ),
            ),
        ),
    )
    bad_registry = CatalogRegistry(
        name="plugin",
        entries=(bad_plugin,),
        id_getter=lambda d: d.plugin_id,
    )
    report = validate_catalog_integrity(plugins=bad_registry)
    assert not report.ok
    assert any(issue.code == "UNKNOWN_FEATURE_REFERENCE" for issue in report.issues)


def test_integrity_rejects_invalid_id_syntax() -> None:
    bad = dataclasses.replace(
        EMA,
        feature_id="Bad-ID",
    )
    with pytest.raises(InvalidCatalogDefinitionError):
        CatalogRegistry(
            name="feature",
            entries=(bad,),
            id_getter=lambda d: d.feature_id,
        )


def test_integrity_report_is_deterministic() -> None:
    a = validate_catalog_integrity()
    b = validate_catalog_integrity()
    assert a == b


def test_import_isolation_no_legacy_side_effects() -> None:
    import sys

    before = set(sys.modules)
    import orderbook_analyse.strategy_lab.catalogs  # noqa: F401

    loaded = set(sys.modules) - before
    forbidden = [
        name
        for name in loaded
        if name.startswith(
            (
                "orderbook_analyse.ema_dual_cross_multisource",
                "orderbook_analyse.cluster_sweep_research",
            )
        )
    ]
    assert forbidden == []
