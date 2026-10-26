"""Provider vs cache readback and golden band parity."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS_D3 = ROOT / "tools/phase_bt_d3_canonical_depth2_provider_v1"
TOOLS_D4 = ROOT / "tools/phase_bt_d4_enforced_depth2_backtester_v1"
TOOLS_D8 = Path(__file__).resolve().parent

for p in (str(TOOLS_D3), str(TOOLS_D4), str(TOOLS_D8)):
    if p not in sys.path:
        sys.path.insert(0, p)

from parity_compare_v1 import compare_band_sets  # noqa: E402
from research_backtester_v1 import ResearchDensityRunV1  # noqa: E402

from cache_builder_v1 import build_cache_bundle  # noqa: E402
from cache_contract_v1 import canonical_snapshot_json, bands_for_parity  # noqa: E402
from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402


_CORE_SNAPSHOT_KEYS = (
    "decision_close_utc",
    "as_of_utc",
    "upper_bands",
    "lower_bands",
    "pipeline_line_id",
    "config_hash",
    "data_provenance",
)


def _core_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    return {k: snap.get(k) for k in _CORE_SNAPSHOT_KEYS}


def parity_provider_vs_cache_snapshot(provider_snap: dict[str, Any], cache_snap: dict[str, Any]) -> dict[str, Any]:
    band_cmp = compare_band_sets(bands_for_parity(provider_snap), bands_for_parity(cache_snap))
    core_provider = _core_snapshot(provider_snap)
    core_cache = _core_snapshot(cache_snap)
    core_equal = canonical_snapshot_json(core_provider) == canonical_snapshot_json(core_cache)
    full_equal = canonical_snapshot_json(provider_snap) == canonical_snapshot_json(cache_snap)
    return {
        "ok": band_cmp["ok"] and core_equal,
        "band_compare": band_cmp,
        "core_snapshot_equal": core_equal,
        "canonical_payload_equal": full_equal,
    }


def parity_at_close(decision_close_utc: str, *, bundle_parent: Path) -> dict[str, Any]:
    session = ResearchDensityRunV1.from_config_path()
    t0 = time.perf_counter()
    provider_snap = session.collect_at_close(decision_close_utc)
    cold_s = time.perf_counter() - t0

    bundle_dir = bundle_parent / f"parity_single_{decision_close_utc.replace(':', '').replace('-', '')}"
    if bundle_dir.exists():
        import shutil

        shutil.rmtree(bundle_dir)
    build_cache_bundle(
        bundle_dir=bundle_dir,
        decision_closes=[decision_close_utc],
        collect_fn=lambda _c: provider_snap,
    )

    t1 = time.perf_counter()
    cache_session = ValidatedDensityCacheSessionV1(bundle_dir)
    cache_snap = cache_session.collect_at_close(decision_close_utc)
    read_s = time.perf_counter() - t1

    cmp = parity_provider_vs_cache_snapshot(provider_snap, cache_snap)
    cmp["decision_close_utc"] = decision_close_utc
    cmp["timing_cold_compute_s"] = round(cold_s, 4)
    cmp["timing_cache_read_s"] = round(read_s, 6)
    return cmp


def parity_against_golden(provider_snap: dict[str, Any], golden_path: Path) -> dict[str, Any]:
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    expected = list(golden.get("upper") or []) + list(golden.get("lower") or [])
    actual = bands_for_parity(provider_snap)
    meta = golden.get("meta") or {}
    band_cmp = compare_band_sets(expected, actual)
    return {
        "ok": band_cmp["ok"],
        "golden_path": str(golden_path),
        "expected_total": meta.get("upper_band_count", 0) + meta.get("lower_band_count", 0),
        "band_compare": band_cmp,
    }


def run_parity_suite(bundle_parent: Path | None = None) -> dict[str, Any]:
    parent = bundle_parent or (ROOT / "results/phase_bt_d8_canonical_density_cache_v1/parity_runs")
    parent.mkdir(parents=True, exist_ok=True)
    session = ResearchDensityRunV1.from_config_path()
    closes = list(session.config.parity_fixture_decision_closes) + list(
        session.config.additional_parity_closes
    )
    results: dict[str, Any] = {"closes": {}, "gates": {}}
    all_ok = True
    golden_path = session.config.golden_reference_path

    for close in closes:
        r = parity_at_close(close, bundle_parent=parent)
        results["closes"][close] = r
        if not r["ok"]:
            all_ok = False

    golden_close = session.config.parity_fixture_decision_closes[0]
    provider_golden = ResearchDensityRunV1.from_config_path().collect_at_close(golden_close)
    gcmp = parity_against_golden(provider_golden, golden_path)
    results["golden_chart_parity"] = gcmp
    if not gcmp["ok"]:
        all_ok = False

    results["gates"]["provider_cache_readback"] = {"ok": all_ok}
    results["gates"]["golden_215_bands"] = {
        "ok": gcmp["ok"],
        "counts": {
            "upper": gcmp["band_compare"]["expected_count"],
            "note": "39 upper + 176 lower canonical",
        },
    }
    results["verdict"] = "DENSITY_CACHE_PARITY_PASS" if all_ok else "DENSITY_CACHE_PARITY_BLOCKED"
    return results
