"""P6 end-to-end StrategySpec V2 YAML files, P4C, compile, and golden artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.compiler_v2 import compile_strategy_v2
from orderbook_analyse.strategy_lab.decoder_v2 import (
    load_compile_strategy_v2,
    load_strategy_v2_yaml,
    load_strategy_v2_yaml_file,
)
from orderbook_analyse.strategy_lab.loader import load_strategy_yaml
from orderbook_analyse.strategy_lab.models.enums import RateUnit
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import AvailabilityTimingV2
from orderbook_analyse.strategy_lab.schema import COMMITTED_SCHEMA_V2_PATH
from orderbook_analyse.strategy_lab.validation import (
    production_catalog_bundle_v2,
    require_valid_strategy_v2_p4c,
    validate_strategy_v2_p4c,
)

REPO = Path(__file__).resolve().parents[2]
STRATEGIES = REPO / "strategies" / "strategy_lab"
EDC_YAML = STRATEGIES / "edc_m0_strict_sync_v2.yaml"
CLUSTER_YAML = STRATEGIES / "cluster_sweep_v2.yaml"
EDC_JSON = STRATEGIES / "compiled" / "edc_m0_strict_sync_v2.canonical.json"
EDC_SHA = STRATEGIES / "compiled" / "edc_m0_strict_sync_v2.sha256"
CLUSTER_JSON = STRATEGIES / "compiled" / "cluster_sweep_v2.canonical.json"
CLUSTER_SHA = STRATEGIES / "compiled" / "cluster_sweep_v2.sha256"

EXPECTED_UNIVERSE_HASH = (
    "sha256:"
    + hashlib.sha256(
        (REPO / "config" / "universe_tradeable_51.json").read_bytes()
    ).hexdigest()
)


@pytest.fixture
def catalogs():
    return production_catalog_bundle_v2()


def _strategy_spec_v2_schema() -> dict[str, object]:
    schema = json.loads(COMMITTED_SCHEMA_V2_PATH.read_text(encoding="utf-8"))
    return {"$defs": schema["$defs"], "$ref": "#/$defs/StrategySpecV2"}


def _read_sha(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text.strip("\n")


@pytest.mark.parametrize(
    ("yaml_path", "json_path", "sha_path"),
    [
        (EDC_YAML, EDC_JSON, EDC_SHA),
        (CLUSTER_YAML, CLUSTER_JSON, CLUSTER_SHA),
    ],
)
def test_yaml_p4c_compile_matches_goldens(
    catalogs, yaml_path: Path, json_path: Path, sha_path: Path
) -> None:
    spec = load_strategy_v2_yaml_file(yaml_path)
    report = validate_strategy_v2_p4c(spec, catalogs)
    assert report.is_valid, report.issues
    compiled = compile_strategy_v2(spec, catalogs)
    assert compiled.canonical_bytes == json_path.read_bytes()
    assert compiled.strategy_hash == _read_sha(sha_path)
    assert re.fullmatch(r"[0-9a-f]{64}", compiled.strategy_hash)
    assert compiled.strategy_hash.encode("ascii") not in compiled.canonical_bytes


def test_load_compile_helper_matches_edc_golden(catalogs) -> None:
    compiled = load_compile_strategy_v2(EDC_YAML, catalogs)
    assert compiled.canonical_bytes == EDC_JSON.read_bytes()
    assert compiled.strategy_hash == _read_sha(EDC_SHA)


def test_compiler_output_validates_against_v2_schema(catalogs) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    for path in (EDC_YAML, CLUSTER_YAML):
        compiled = load_compile_strategy_v2(path, catalogs)
        payload = json.loads(compiled.canonical_bytes)
        jsonschema.validate(payload, _strategy_spec_v2_schema())


def test_second_compile_identical(catalogs) -> None:
    spec = load_strategy_v2_yaml_file(EDC_YAML)
    a = compile_strategy_v2(spec, catalogs)
    b = compile_strategy_v2(spec, catalogs)
    assert a.canonical_bytes == b.canonical_bytes
    assert a.strategy_hash == b.strategy_hash


def test_cross_process_determinism() -> None:
    script = r"""
from orderbook_analyse.strategy_lab.decoder_v2 import load_compile_strategy_v2
from orderbook_analyse.strategy_lab.validation import production_catalog_bundle_v2
c = production_catalog_bundle_v2()
edc = load_compile_strategy_v2(%r, c)
cluster = load_compile_strategy_v2(%r, c)
print(edc.strategy_hash)
print(edc.canonical_bytes.hex())
print(cluster.strategy_hash)
print(cluster.canonical_bytes.hex())
""" % (
        str(EDC_YAML),
        str(CLUSTER_YAML),
    )
    runs = []
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(REPO),
            env={**dict(**__import__("os").environ), "PYTHONPATH": "src"},
        )
        runs.append(proc.stdout.strip().splitlines())
    assert runs[0] == runs[1]


def test_yaml_mapping_key_order_does_not_change_hash(catalogs) -> None:
    raw = load_strategy_yaml(EDC_YAML)
    # Rebuild mapping with reversed top-level key insertion order.
    reversed_map = {k: raw[k] for k in reversed(list(raw.keys()))}
    from orderbook_analyse.strategy_lab.decoder_v2 import decode_strategy_v2

    a = compile_strategy_v2(decode_strategy_v2(raw), catalogs)
    b = compile_strategy_v2(decode_strategy_v2(reversed_map), catalogs)
    assert a.strategy_hash == b.strategy_hash
    assert a.canonical_bytes == b.canonical_bytes


def test_yaml_sequence_order_can_change_hash(catalogs) -> None:
    from orderbook_analyse.strategy_lab.decoder_v2 import decode_strategy_v2

    raw = copy.deepcopy(load_strategy_yaml(EDC_YAML))
    swapped = copy.deepcopy(raw)
    dims = swapped["research_parameter_space"]["dimensions"]
    assert len(dims) >= 2
    dims[0], dims[1] = dims[1], dims[0]
    a = compile_strategy_v2(decode_strategy_v2(raw), catalogs)
    b = compile_strategy_v2(decode_strategy_v2(swapped), catalogs)
    assert a.strategy_hash != b.strategy_hash


def test_strategy_value_change_changes_hash(catalogs) -> None:
    from orderbook_analyse.strategy_lab.decoder_v2 import decode_strategy_v2

    raw = copy.deepcopy(load_strategy_yaml(EDC_YAML))
    changed = copy.deepcopy(raw)
    changed["costs"]["roundtrip_cost"]["value"] = Decimal("0.20")
    a = compile_strategy_v2(decode_strategy_v2(raw), catalogs)
    b = compile_strategy_v2(decode_strategy_v2(changed), catalogs)
    assert a.strategy_hash != b.strategy_hash


def test_edc_feature_contract_matches_plugin_required_features(catalogs) -> None:
    """Freeze the current catalog contract: ema_fast=9, ema_slow=20, atr=14 only."""
    from orderbook_analyse.strategy_lab.catalogs.v2.registry import PLUGIN_CATALOG_V2

    plugin = PLUGIN_CATALOG_V2.get("edc_m0_strict_sync")
    required = {
        req.alias.value: (
            req.feature_id.value,
            req.bindings[0].name.value,
            req.bindings[0].value.value,
        )
        for req in plugin.required_features
    }
    assert required == {
        "ema_fast": ("ema", "period", 9),
        "ema_slow": ("ema", "period", 20),
        "atr": ("atr_wilder", "period", 14),
    }
    assert "ema_medium" not in required
    assert all(period != 59 for _, _, period in required.values())

    edc = load_strategy_v2_yaml_file(EDC_YAML)
    require_valid_strategy_v2_p4c(edc, catalogs)
    bound = {
        f.alias.value: (
            f.catalog_feature_id.value,
            f.bindings[0].name.value,
            f.bindings[0].value.value,
        )
        for f in edc.features
    }
    assert bound == required
    assert [f.alias.value for f in edc.features] == [
        "ema_fast",
        "ema_slow",
        "atr",
    ]


def test_edc_baseline_and_research_values(catalogs) -> None:
    edc = load_strategy_v2_yaml_file(EDC_YAML)
    require_valid_strategy_v2_p4c(edc, catalogs)
    assert edc.exit.take_profit.value == Decimal("0.75")
    assert edc.exit.stop_loss.value == Decimal("0.5") or edc.exit.stop_loss.value == Decimal(
        "0.50"
    )
    assert edc.exit.horizon.value == Decimal("8")
    assert edc.costs.roundtrip_cost.value == Decimal("0.15")
    assert edc.execution_assumptions.fixed_notional == Decimal("1000")
    assert edc.timeframes.signal.value == 5
    assert edc.timeframes.execution.value == 1
    dims = {
        d.dimension_id.value: d for d in edc.research_parameter_space.dimensions
    }
    tp_vals = [c.value.value for c in dims["take_profit"].candidates]
    assert tp_vals == [
        Decimal("0.40"),
        Decimal("0.50"),
        Decimal("0.60"),
        Decimal("0.75"),
    ]
    sl_vals = [c.value.value for c in dims["stop_loss"].candidates]
    assert sl_vals == [Decimal("0.50"), Decimal("1.00")] or sl_vals == [
        Decimal("0.5"),
        Decimal("1"),
    ]
    h_vals = [c.value.value for c in dims["horizon"].candidates]
    assert h_vals == [Decimal("4"), Decimal("6"), Decimal("8")]
    cost_vals = [c.value.value for c in dims["roundtrip_cost"].candidates]
    assert cost_vals == [
        Decimal("0.11"),
        Decimal("0.15"),
        Decimal("0.20"),
    ]


def test_cluster_semantics(catalogs) -> None:
    cluster = load_strategy_v2_yaml_file(CLUSTER_YAML)
    require_valid_strategy_v2_p4c(cluster, catalogs)
    assert (
        cluster.entry.signal_decision_timing
        == AvailabilityTimingV2.CONFIRMATION_BAR_CLOSE
    )
    assert cluster.research_parameter_space.dimensions == ()
    clusters = next(f for f in cluster.features if f.alias.value == "clusters")
    gap = next(b for b in clusters.bindings if b.name.value == "gap_pct")
    pools = next(b for b in clusters.bindings if b.name.value == "minimum_pools")
    assert gap.value.value.value == Decimal("0.10") or gap.value.value.value == Decimal(
        "0.1"
    )
    assert gap.value.value.unit == RateUnit.PERCENT
    assert pools.value.value == 3
    assert cluster.portfolio_assumptions.compounding is False


def test_universe_hash_and_provenance() -> None:
    file_digest = hashlib.sha256(
        (REPO / "config" / "universe_tradeable_51.json").read_bytes()
    ).hexdigest()
    assert EXPECTED_UNIVERSE_HASH == f"sha256:{file_digest}"
    assert file_digest == (
        "796ace7b68178a52279aee256ea7c1a109a3aa780b8ebf36d965f89a300b49bb"
    )
    for path in (EDC_YAML, CLUSTER_YAML):
        spec = load_strategy_v2_yaml_file(path)
        assert spec.universe.content_hash == EXPECTED_UNIVERSE_HASH
        assert spec.universe.universe_id.value == "tradeable_51"
        assert spec.provenance.catalog_contract_version.value == "catalog/v2"
        assert len(spec.provenance.plugin_refs) == 1
        assert spec.provenance.plugin_refs[0].contract_version.value == "catalog/v2"
        assert spec.provenance.git_commit
        assert spec.provenance.source_repository == "orderbook_analyse"


def test_no_baseline_mirror_object_in_edc() -> None:
    raw = load_strategy_yaml(EDC_YAML)
    assert "baseline" not in raw
    assert "baseline" not in json.loads(EDC_JSON.read_bytes())
