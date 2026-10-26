from __future__ import annotations

import json
from pathlib import Path

import pytest

from density_provider_v1 import DensityProviderError, get_density_bands
from parity_compare_v1 import compare_band_sets

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "results/phase_bt_d3_canonical_depth2_provider_v1/golden_20260925_1100_chart_path.json"
LEGACY = ROOT / "results/phase_density_chart_parity_20260925_1100/pools.json"

requires_ssot = pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard").is_dir(),
    reason="spread dashboard SSOT path missing",
)


@requires_ssot
def test_sep25_chart_path_band_counts():
    r = get_density_bands("DOGEUSDT", "2026-09-25T11:00:00Z", use_parity_fixture_window=True)
    assert len(r.upper) == 39
    assert len(r.lower) == 176
    assert len(r.bands) == 215
    assert r.manifest["min_unique_origins"] == 2
    assert r.manifest["density_method"] == "exact_unique_origin"


@requires_ssot
def test_sep25_matches_golden_when_present():
    if not GOLDEN.is_file():
        pytest.skip("run run_parity_suite.py to generate golden")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    r = get_density_bands("DOGEUSDT", "2026-09-25T11:00:00Z", use_parity_fixture_window=True)
    cmp = compare_band_sets(golden.get("upper", []) + golden.get("lower", []), r.bands)
    assert cmp["ok"], cmp


@requires_ssot
def test_legacy_python_reference_diff_documented():
    if not LEGACY.is_file():
        pytest.skip("legacy reference missing")
    legacy = json.loads(LEGACY.read_text(encoding="utf-8"))
    r = get_density_bands("DOGEUSDT", "2026-09-25T11:00:00Z", use_parity_fixture_window=True)
    rows = (legacy.get("upper") or []) + (legacy.get("lower") or [])
    cmp = compare_band_sets(rows, r.bands)
    # Chart-path JS canonical ≠ Python sweep export (179 vs 176 lowers)
    assert cmp["ok"] is False
    assert cmp["only_expected_total"] >= 3


@requires_ssot
def test_out_of_scope_fail_closed():
    with pytest.raises(DensityProviderError) as ei:
        get_density_bands("DOGEUSDT", "2025-01-01T12:00:00Z")
    assert ei.value.code in ("OUT_OF_SCOPE", "INVALID_REQUEST")


@requires_ssot
def test_consecutive_closes_deterministic():
    as_ofs = ["2026-09-25T10:50:00Z", "2026-09-25T10:55:00Z", "2026-09-25T11:00:00Z"]
    snapshots = []
    for as_of in as_ofs:
        a = get_density_bands("DOGEUSDT", as_of)
        b = get_density_bands("DOGEUSDT", as_of)
        assert a.bands == b.bands
        snapshots.append(json.dumps(a.bands, sort_keys=True))
    assert len(set(snapshots)) >= 2
