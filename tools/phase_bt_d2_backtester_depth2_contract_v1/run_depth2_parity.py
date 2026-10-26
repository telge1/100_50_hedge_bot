#!/usr/bin/env python3
"""
PHASE BT-D2 — read-only parity gate: chart/reference Exact-Density bands vs optional backtester export.

Does not run a backtest. Does not modify the backtester.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REF_JSON = ROOT / "results/phase_density_chart_parity_20260925_1100/pools.json"
EXTRACT = ROOT / "tools/phase_density_chart_parity_20260925_1100/extract_bands.py"
ANALYZER = ROOT / "tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js"
DEFAULT_AS_OF = "2026-09-25T11:00:00Z"
OUT = ROOT / "results/phase_bt_d2_backtester_depth2_contract_v1"


def _band_key(side: str, lo: str, hi: str) -> tuple[str, str, str]:
    return (side.lower(), str(lo), str(hi))


def load_reference(path: Path) -> tuple[dict, list[dict], list[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    meta = payload.get("meta") or {}
    upper = payload.get("upper") or []
    lower = payload.get("lower") or []
    return meta, upper, lower


def recompute_chart_reference(as_of: str) -> dict:
    """Re-run frozen extract_bands (SSOT window + sweep parity used for Phase density export)."""
    proc = subprocess.run(
        [sys.executable, str(EXTRACT)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "extract_bands failed")
    return json.loads(REF_JSON.read_text(encoding="utf-8"))


def compare_band_lists(
    label_a: str,
    bands_a: list[dict],
    label_b: str,
    bands_b: list[dict],
) -> dict:
    def norm(rows: list[dict], side_key: str) -> dict[tuple[str, str, str], dict]:
        out: dict[tuple[str, str, str], dict] = {}
        for r in rows:
            side = str(r.get("side", "")).lower()
            if side.startswith("u"):
                side = "upper"
            elif side.startswith("l"):
                side = "lower"
            lo = r.get("lower_price") or r.get("bottom_price")
            hi = r.get("upper_price") or r.get("top_price")
            if lo is None or hi is None:
                continue
            key = _band_key(side, str(lo), str(hi))
            out[key] = {
                "side": side,
                "lower_price": str(lo),
                "upper_price": str(hi),
                "unique_origin_count": int(
                    r.get("unique_origin_count") or r.get("depth") or 0
                ),
                "origin_ids": r.get("origin_ids")
                or "|".join(sorted(r.get("origins") or [])),
            }
        return out

    ma = norm(bands_a, "side")
    mb = norm(bands_b, "side")
    only_a = sorted(set(ma) - set(mb))
    only_b = sorted(set(mb) - set(ma))
    mismatches = []
    for k in sorted(set(ma) & set(mb)):
        if ma[k]["unique_origin_count"] != mb[k]["unique_origin_count"]:
            mismatches.append(
                {
                    "band": k,
                    "unique_origin_count": {label_a: ma[k]["unique_origin_count"], label_b: mb[k]["unique_origin_count"]},
                }
            )
        elif ma[k]["origin_ids"] != mb[k]["origin_ids"]:
            mismatches.append(
                {
                    "band": k,
                    "origin_ids": {label_a: ma[k]["origin_ids"], label_b: mb[k]["origin_ids"]},
                }
            )
    return {
        "ok": not only_a and not only_b and not mismatches,
        "count_a": len(ma),
        "count_b": len(mb),
        "only_in_a": [ma[k] for k in only_a[:20]],
        "only_in_b": [mb[k] for k in only_b[:20]],
        "only_in_a_total": len(only_a),
        "only_in_b_total": len(only_b),
        "field_mismatches": mismatches[:50],
        "field_mismatch_total": len(mismatches),
    }


def load_backtester_manifest(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="BT-D2 depth-2 band parity (read-only)")
    parser.add_argument("--as-of", default=DEFAULT_AS_OF)
    parser.add_argument("--reference", type=Path, default=REF_JSON)
    parser.add_argument(
        "--backtester-manifest",
        type=Path,
        default=None,
        help="Future: JSON with upper/lower exact-density bands per decision close",
    )
    parser.add_argument("--skip-recompute", action="store_true")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    ref_meta, ref_upper, ref_lower = load_reference(args.reference)
    expected_upper = int(ref_meta.get("upper_band_count") or len(ref_upper))
    expected_lower = int(ref_meta.get("lower_band_count") or len(ref_lower))
    expected_total = expected_upper + expected_lower

    report: dict = {
        "phase": "BT-D2",
        "as_of_utc": args.as_of,
        "reference_path": str(args.reference),
        "expected": {
            "upper_bands": expected_upper,
            "lower_bands": expected_lower,
            "total_bands": expected_total,
            "min_unique_origins": ref_meta.get("min_unique_origins", 2),
        },
        "gates": {},
    }

    if not args.skip_recompute:
        fresh = recompute_chart_reference(args.as_of)
        fresh_upper = fresh.get("upper") or []
        fresh_lower = fresh.get("lower") or []
        report["gates"]["reference_vs_fresh_recompute"] = compare_band_lists(
            "reference",
            ref_upper + ref_lower,
            "fresh_recompute",
            fresh_upper + fresh_lower,
        )
    else:
        report["gates"]["reference_vs_fresh_recompute"] = {"ok": None, "skipped": True}

    bt = load_backtester_manifest(args.backtester_manifest)
    if bt is None:
        report["gates"]["backtester_vs_reference"] = {
            "ok": False,
            "blocked": True,
            "reason": "BACKTESTER_DENSITY_MANIFEST_MISSING",
            "hint": (
                "Backtester does not yet emit per-close exact-density manifests; "
                "wire build_landscape_window + chart-path depth2 export before this gate can PASS."
            ),
        }
    else:
        bt_upper = bt.get("upper") or bt.get("upper_bands") or []
        bt_lower = bt.get("lower") or bt.get("lower_bands") or []
        report["gates"]["backtester_vs_reference"] = compare_band_lists(
            "reference",
            ref_upper + ref_lower,
            "backtester",
            bt_upper + bt_lower,
        )

    all_ok = all(
        g.get("ok") is True
        for g in report["gates"].values()
        if not g.get("skipped")
    )
    report["verdict"] = "PARITY_PASS" if all_ok else "PARITY_FAIL_OR_BLOCKED"

    out_path = OUT / "parity_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "path": str(out_path)}, indent=2))
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
