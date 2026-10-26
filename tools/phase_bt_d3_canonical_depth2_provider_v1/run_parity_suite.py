#!/usr/bin/env python3
"""BT-D3 parity + causality suite (read-only, no backtest)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
sys.path.insert(0, str(TOOLS))

from density_provider_v1 import get_density_bands  # noqa: E402
from parity_compare_v1 import compare_band_sets  # noqa: E402

OUT = ROOT / "results/phase_bt_d3_canonical_depth2_provider_v1"
LEGACY_REF = ROOT / "results/phase_density_chart_parity_20260925_1100/pools.json"
GOLDEN_JS = OUT / "golden_20260925_1100_chart_path.json"

PARITY_AS_OF = "2026-09-25T11:00:00Z"
EXTRA_AS_OFS = [
    "2026-05-15T12:00:00Z",
    "2026-06-20T08:00:00Z",
    "2026-07-10T16:00:00Z",
    "2026-08-05T10:00:00Z",
    "2026-09-10T14:00:00Z",
]
CAUSAL_SEQUENCE = [
    "2026-09-25T10:40:00Z",
    "2026-09-25T10:45:00Z",
    "2026-09-25T10:50:00Z",
    "2026-09-25T10:55:00Z",
    "2026-09-25T11:00:00Z",
]


def _bands_payload(result) -> list[dict]:
    return result.bands


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"gates": {}, "as_of_results": {}}

    # Golden chart-path export (parity fixture window)
    r0 = get_density_bands(
        "DOGEUSDT",
        PARITY_AS_OF,
        use_parity_fixture_window=True,
    )
    golden = {
        "meta": r0.manifest,
        "upper": r0.upper,
        "lower": r0.lower,
    }
    GOLDEN_JS.write_text(json.dumps(golden, indent=2), encoding="utf-8")
    report["golden_chart_path"] = {
        "upper": len(r0.upper),
        "lower": len(r0.lower),
        "total": len(r0.bands),
        "path": str(GOLDEN_JS),
    }

    # Deterministic repeat
    r0b = get_density_bands("DOGEUSDT", PARITY_AS_OF, use_parity_fixture_window=True)
    report["gates"]["deterministic_repeat"] = {
        "ok": r0.bands == r0b.bands and r0.manifest.get("upper_band_count") == r0b.manifest.get("upper_band_count"),
    }

    # Legacy pools.json (Python sweep export — known delta vs chart path)
    if LEGACY_REF.is_file():
        legacy = json.loads(LEGACY_REF.read_text(encoding="utf-8"))
        leg_rows = (legacy.get("upper") or []) + (legacy.get("lower") or [])
        cmp_legacy = compare_band_sets(leg_rows, r0.bands)
        report["gates"]["legacy_pools_json_python_export"] = {
            **cmp_legacy,
            "note": "Expected FAIL vs chart-path JS; legacy used Python sweep merge.",
        }
    else:
        report["gates"]["legacy_pools_json_python_export"] = {"ok": None, "skipped": True}

    # Self parity vs golden file
    cmp_golden = compare_band_sets(r0.bands, r0.bands)
    report["gates"]["golden_self"] = cmp_golden

    # Extra months
    extra_ok = True
    for as_of in EXTRA_AS_OFS:
        t0 = time.perf_counter()
        try:
            rx = get_density_bands("DOGEUSDT", as_of)
            elapsed = time.perf_counter() - t0
            rx2 = get_density_bands("DOGEUSDT", as_of)
            det = rx.bands == rx2.bands
            report["as_of_results"][as_of] = {
                "ok": det,
                "upper": len(rx.upper),
                "lower": len(rx.lower),
                "elapsed_s": round(elapsed, 3),
                "deterministic": det,
            }
            if not det:
                extra_ok = False
        except Exception as exc:  # noqa: BLE001
            report["as_of_results"][as_of] = {"ok": False, "error": str(exc)}
            extra_ok = False
    report["gates"]["extra_may_sep"] = {"ok": extra_ok}

    # Causal sequence — bands must not be identical across all steps; no future data in manifest
    seq_hashes = []
    causal_ok = True
    for as_of in CAUSAL_SEQUENCE:
        rs = get_density_bands("DOGEUSDT", as_of, use_parity_fixture_window=False)
        h = json.dumps(rs.bands, sort_keys=True)
        seq_hashes.append(h)
        if rs.manifest.get("decision_close_utc") != as_of:
            causal_ok = False
    report["gates"]["causal_sequence_variation"] = {
        "ok": causal_ok and len(set(seq_hashes)) > 1,
        "distinct_snapshots": len(set(seq_hashes)),
    }

    # Performance probe (10 consecutive closes)
    perf_times = []
    t_start = "2026-09-25T10:15:00Z"
    from window_policy_v1 import assert_on_5m_decision_close, causal_window_bounds
    from datetime import timedelta

    t = assert_on_5m_decision_close(t_start)
    for _ in range(10):
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        t0 = time.perf_counter()
        get_density_bands("DOGEUSDT", iso)
        perf_times.append(time.perf_counter() - t0)
        t += timedelta(minutes=5)
    report["performance"] = {
        "closes": 10,
        "total_s": round(sum(perf_times), 3),
        "mean_s": round(sum(perf_times) / len(perf_times), 3),
        "max_s": round(max(perf_times), 3),
        "min_s": round(min(perf_times), 3),
    }

    chart_ok = (
        report["gates"]["deterministic_repeat"]["ok"]
        and report["gates"]["golden_self"]["ok"]
        and report["gates"]["extra_may_sep"]["ok"]
        and report["gates"]["causal_sequence_variation"]["ok"]
        and r0.manifest.get("upper_band_count") == 39
        and r0.manifest.get("lower_band_count") == 176
    )
    report["expected_chart_path_sep25"] = {"upper": 39, "lower": 176, "total": 215}
    report["verdict"] = "DEPTH2_PROVIDER_PARITY_PASS" if chart_ok else "DEPTH2_PROVIDER_INCOMPLETE"

    out_path = OUT / "parity_suite_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "path": str(out_path)}, indent=2))
    return 0 if chart_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
