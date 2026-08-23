"""P1 construct / contract tests for StrategySpec V1 (checklist 1–20)."""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from dataclasses import FrozenInstanceError, fields, is_dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.models import (
    STRATEGY_SPEC_SCHEMA_VERSION,
    CausalityStatus,
    DurationUnit,
    DurationValue,
    ExitMode,
    ExitSpec,
    FeesSpec,
    MirrorMode,
    ModelingStatus,
    PluginKind,
    PluginRef,
    RateUnit,
    RateValue,
    SideName,
    SideSpec,
    StrategySpec,
    TimeframeUnit,
    TimeframeValue,
)
from tests.strategy_lab.conftest import (
    cluster_shaped_strategy_spec,
    edc_shaped_strategy_spec,
    minimal_strategy_spec,
)

STRATEGY_LAB_SRC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "orderbook_analyse"
    / "strategy_lab"
)
FORBIDDEN_IMPORT_ROOTS = (
    "orderbook_analyse.ema_dual_cross_multisource",
    "orderbook_analyse.cluster_sweep_research",
    "orderbook_analyse.fake_impulse_filter",
)


# ---------------------------------------------------------------------------
# 1. vollständige minimale StrategySpec konstruierbar
# 14. Package-Import funktioniert
# ---------------------------------------------------------------------------
def test_01_minimal_strategy_spec_constructible() -> None:
    spec = minimal_strategy_spec()
    assert isinstance(spec, StrategySpec)
    assert spec.metadata.schema_version == STRATEGY_SPEC_SCHEMA_VERSION
    assert spec.provenance is not None


def test_14_package_import_works() -> None:
    import orderbook_analyse.strategy_lab as pkg
    import orderbook_analyse.strategy_lab.models as models

    assert pkg.StrategySpec is StrategySpec
    assert models.STRATEGY_SPEC_SCHEMA_VERSION == STRATEGY_SPEC_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 2. EDC-artige Struktur ohne Legacy-Import
# 3. Cluster-Sweep-artige Struktur
# ---------------------------------------------------------------------------
def test_02_edc_shaped_without_legacy_import() -> None:
    before = {m for m in sys.modules if any(m.startswith(f) for f in FORBIDDEN_IMPORT_ROOTS)}
    spec = edc_shaped_strategy_spec()
    after = {m for m in sys.modules if any(m.startswith(f) for f in FORBIDDEN_IMPORT_ROOTS)}
    assert after == before
    assert spec.metadata.family == "ema_dual_cross"
    assert spec.exit.take_profit == RateValue(
        value=Decimal("0.75"), unit=RateUnit.PERCENT
    )
    assert spec.timeframes.signal == TimeframeValue(
        value=5, unit=TimeframeUnit.MINUTES
    )


def test_03_cluster_sweep_shaped() -> None:
    spec = cluster_shaped_strategy_spec()
    assert spec.metadata.family == "cluster_sweep"
    assert spec.exit.take_profit == RateValue(
        value=Decimal("0.005"), unit=RateUnit.FRACTION
    )
    assert spec.timeframes.signal == TimeframeValue(
        value=15, unit=TimeframeUnit.MINUTES
    )
    assert spec.fees.roundtrip_cost.unit is RateUnit.BASIS_POINTS


# ---------------------------------------------------------------------------
# 4. 0.75 Percent und 0.0075 Fraction eindeutig
# 15. Fee in Basispunkten eindeutig
# ---------------------------------------------------------------------------
def test_04_percent_and_fraction_unambiguous() -> None:
    pct = RateValue(value=Decimal("0.75"), unit=RateUnit.PERCENT)
    frac = RateValue(value=Decimal("0.0075"), unit=RateUnit.FRACTION)
    cluster = RateValue(value=Decimal("0.005"), unit=RateUnit.FRACTION)
    assert pct.value == Decimal("0.75")
    assert pct.unit is RateUnit.PERCENT
    assert frac.value == Decimal("0.0075")
    assert frac.unit is RateUnit.FRACTION
    assert cluster.unit is RateUnit.FRACTION
    assert pct != frac  # same economic size, different typed representation
    assert isinstance(pct.value, Decimal)
    assert isinstance(frac.value, Decimal)


def test_15_fee_basis_points_unambiguous() -> None:
    fee = RateValue(value=Decimal("15"), unit=RateUnit.BASIS_POINTS)
    fees = FeesSpec(roundtrip_cost=fee)
    assert fees.roundtrip_cost.unit is RateUnit.BASIS_POINTS
    assert fees.roundtrip_cost.value == Decimal("15")


# ---------------------------------------------------------------------------
# 5. 8 Hours und 480 Minutes eindeutig
# ---------------------------------------------------------------------------
def test_05_duration_hours_and_minutes_unambiguous() -> None:
    eight_h = DurationValue(value=Decimal("8"), unit=DurationUnit.HOURS)
    four80_m = DurationValue(value=Decimal("480"), unit=DurationUnit.MINUTES)
    assert eight_h != four80_m  # no canonicalization in P1
    assert eight_h.unit is DurationUnit.HOURS
    assert four80_m.unit is DurationUnit.MINUTES
    sig = TimeframeValue(value=5, unit=TimeframeUnit.MINUTES)
    exe = TimeframeValue(value=1, unit=TimeframeUnit.MINUTES)
    cluster_sig = TimeframeValue(value=15, unit=TimeframeUnit.MINUTES)
    assert sig.value == 5 and exe.value == 1 and cluster_sig.value == 15
    # timeframe ≠ holding duration (different types)
    assert type(sig) is not type(eight_h)


# ---------------------------------------------------------------------------
# 6. fehlende Pflichtfelder → TypeError
# 7. Long und Short erforderlich
# 18. Provenance ist Pflicht
# ---------------------------------------------------------------------------
def test_06_missing_required_fields_type_error() -> None:
    with pytest.raises(TypeError):
        StrategySpec()  # type: ignore[call-arg]


def test_07_long_and_short_required() -> None:
    required = {f.name for f in fields(StrategySpec)}
    assert "long" in required and "short" in required
    with pytest.raises(TypeError):
        StrategySpec(  # type: ignore[call-arg]
            metadata=minimal_strategy_spec().metadata,
        )


def test_18_provenance_required() -> None:
    required = {f.name for f in fields(StrategySpec)}
    assert "provenance" in required
    with pytest.raises(TypeError):
        StrategySpec(  # type: ignore[call-arg]
            metadata=minimal_strategy_spec().metadata,
            universe=minimal_strategy_spec().universe,
        )


# ---------------------------------------------------------------------------
# 8. Mirror ist explizit
# ---------------------------------------------------------------------------
def test_08_mirror_is_explicit() -> None:
    spec = minimal_strategy_spec()
    assert isinstance(spec.short.mirror_mode, MirrorMode)
    assert spec.short.mirror_mode is MirrorMode.FULL_MIRROR
    assert spec.short.mirror_of is SideName.LONG
    flipped = minimal_strategy_spec(
        short=SideSpec(
            name=SideName.SHORT,
            mirror_mode=MirrorMode.SIGN_FLIP,
            mirror_of=SideName.LONG,
            sign_flip_fields=("direction",),
        )
    )
    assert flipped.short.mirror_mode is MirrorMode.SIGN_FLIP


# ---------------------------------------------------------------------------
# 9. Slippage NOT_MODELED
# 10. Funding UNAVAILABLE
# ---------------------------------------------------------------------------
def test_09_slippage_not_modeled_typed() -> None:
    assert minimal_strategy_spec().slippage.status is ModelingStatus.NOT_MODELED


def test_10_funding_unavailable_typed() -> None:
    assert minimal_strategy_spec().funding.status is ModelingStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# 11. Sequenzen sind Tupel
# 12. keine mutable Defaults
# 20. frozen assignment scheitert
# ---------------------------------------------------------------------------
def test_11_sequences_are_tuples() -> None:
    spec = minimal_strategy_spec()
    assert isinstance(spec.data_requirements, tuple)
    assert isinstance(spec.features, tuple)
    assert isinstance(spec.research_parameter_space.cells, tuple)
    assert isinstance(spec.provenance.plugin_refs, tuple)
    assert isinstance(spec.provenance.external_runtime_dependencies, tuple)
    with pytest.raises(AttributeError):
        spec.features.append(spec.features[0])  # type: ignore[attr-defined]


def test_12_no_mutable_defaults_shared() -> None:
    a = PluginRef(id="a", version="1", kind=PluginKind.OTHER)
    b = PluginRef(id="b", version="1", kind=PluginKind.OTHER)
    assert a.config == ()
    assert b.config == ()
    assert type(a.config) is tuple
    with pytest.raises(AttributeError):
        a.config.append("x")  # type: ignore[attr-defined]
    # rebinding a frozen field fails (instances do not share a mutable default)
    with pytest.raises(FrozenInstanceError):
        a.config = ()  # type: ignore[misc]


def test_20_frozen_assignment_fails() -> None:
    spec = minimal_strategy_spec()
    with pytest.raises(FrozenInstanceError):
        spec.metadata = spec.metadata  # type: ignore[misc]
    rate = RateValue(value=Decimal("1"), unit=RateUnit.PERCENT)
    with pytest.raises(FrozenInstanceError):
        rate.value = Decimal("2")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 13. keine Legacy-EDC-/Cluster-Imports (static + runtime)
# ---------------------------------------------------------------------------
def test_13_no_legacy_imports_static_and_runtime() -> None:
    for path in STRATEGY_LAB_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in FORBIDDEN_IMPORT_ROOTS:
                        assert not alias.name.startswith(forbidden), path
            elif isinstance(node, ast.ImportFrom) and node.module:
                for forbidden in FORBIDDEN_IMPORT_ROOTS:
                    assert not node.module.startswith(forbidden), path

    before = set(sys.modules)
    importlib.reload(importlib.import_module("orderbook_analyse.strategy_lab.models"))
    after = set(sys.modules) - before
    for name in after:
        for forbidden in FORBIDDEN_IMPORT_ROOTS:
            assert not name.startswith(forbidden), name


# ---------------------------------------------------------------------------
# 16. Entry decision / tradable getrennt
# 17. Setup / Trigger / Confirmation getrennt
# 19. Baseline und Research Space getrennt
# ---------------------------------------------------------------------------
def test_16_entry_decision_and_tradable_separated() -> None:
    spec = minimal_strategy_spec()
    assert spec.entry.decision_point == "signal_bar_close"
    assert spec.entry.tradable_point == "next_execution_bar_open"
    assert spec.entry.decision_point != spec.entry.tradable_point
    names = {f.name for f in fields(type(spec.entry))}
    assert "decision_point" in names and "tradable_point" in names


def test_17_setup_trigger_confirmation_separated() -> None:
    spec = minimal_strategy_spec()
    assert spec.setup.description
    assert spec.trigger.description
    assert spec.confirmation.description
    assert type(spec.setup).__name__ == "SetupSpec"
    assert type(spec.trigger).__name__ == "TriggerSpec"
    assert type(spec.confirmation).__name__ == "ConfirmationSpec"


def test_19_baseline_and_research_space_separated() -> None:
    spec = edc_shaped_strategy_spec()
    assert type(spec.baseline).__name__ == "BaselineSpec"
    assert type(spec.research_parameter_space).__name__ == "ResearchParameterSpace"
    assert spec.baseline.cell.cell_id == "edc_5m_0.75pct_8h"
    assert len(spec.research_parameter_space.cells) >= 2


# ---------------------------------------------------------------------------
# Extra contract: plugin exit, Decimal not float, kw_only/slots/frozen
# ---------------------------------------------------------------------------
def test_plugin_only_exit_expressible() -> None:
    exit_spec = ExitSpec(
        mode=ExitMode.PLUGIN,
        plugin=PluginRef(id="exit.custom", version="1.0.0", kind=PluginKind.EXIT),
    )
    assert exit_spec.take_profit is None
    assert exit_spec.plugin is not None


def test_financial_fields_use_decimal_not_float() -> None:
    spec = minimal_strategy_spec()
    assert isinstance(spec.exit.take_profit.value, Decimal)  # type: ignore[union-attr]
    assert isinstance(spec.execution_assumptions.notional, Decimal)
    assert not isinstance(spec.exit.take_profit.value, float)  # type: ignore[union-attr]


def test_dataclasses_are_frozen_slots_kwonly() -> None:
    import orderbook_analyse.strategy_lab.models.provenance as prov_mod
    import orderbook_analyse.strategy_lab.models.strategy as strategy_mod

    for mod in (strategy_mod, prov_mod):
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if obj.__module__ != mod.__name__:
                continue
            if not is_dataclass(obj):
                continue
            params = getattr(obj, "__dataclass_params__")
            assert params.frozen is True, obj
            assert getattr(obj, "__slots__", None) is not None, obj
            for f in fields(obj):
                assert f.kw_only is True, (obj, f.name)


def test_fees_slippage_funding_are_separate_top_level() -> None:
    names = {f.name for f in fields(StrategySpec)}
    assert {"fees", "slippage", "funding"} <= names
    spec = minimal_strategy_spec()
    assert spec.fees is not spec.slippage
    assert spec.funding.status is ModelingStatus.UNAVAILABLE


def test_provenance_expresses_required_claims() -> None:
    p = minimal_strategy_spec().provenance
    assert p.source_of_truth_module
    assert p.source_of_truth_path
    assert p.git_commit
    assert p.strategy_ref
    assert p.policy_ref
    assert isinstance(p.plugin_refs, tuple)
    assert isinstance(p.causality_status, CausalityStatus)
    assert p.causality_claim
    assert "TRP" in p.external_runtime_dependencies
    assert isinstance(p.known_limitations, tuple)
    assert isinstance(p.notes, tuple)
