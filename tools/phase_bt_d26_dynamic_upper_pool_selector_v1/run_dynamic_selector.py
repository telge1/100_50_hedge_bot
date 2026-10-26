#!/usr/bin/env python3
"""CLI — BT-D26 dynamic upper pool selector (does not modify BT-D23 runs)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
BT_D23 = ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1"
BT_D26 = Path(__file__).resolve().parent
BT_P4D = ROOT / "tools" / "phase4d_pool_landscape_backtester_v2"
for p in (str(BT_D6), str(BT_D7), str(BT_D8), str(BT_D23), str(BT_D26), str(BT_P4D)):
    if p not in sys.path:
        sys.path.insert(0, p)

from chart_adapter_v1 import setups_document_to_overlay_bundle  # noqa: E402
from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from dynamic_selector_v1 import result_to_json_dict, scan_dynamic_upper_pool_setups  # noqa: E402


def _write_csv(path: Path, setups: list[dict]) -> None:
    if not setups:
        path.write_text("", encoding="utf-8")
        return
    fields = list(setups[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in setups:
            flat = dict(row)
            for k in ("as_of_snapshot", "density_provenance", "previous_candidate"):
                if isinstance(flat.get(k), dict):
                    flat[k] = json.dumps(flat[k], sort_keys=True)
            w.writerow(flat)


def main() -> int:
    ap = argparse.ArgumentParser(description="BT-D26 dynamic upper pool selector")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--compare-bt-d23",
        type=Path,
        default=None,
        help="Path to existing setups.json for A/B counts",
    )
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args()

    cfg = load_yaml_and_validate(args.config.resolve())
    out_dir = ROOT / "results" / "setup_scanner_runs" / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    d23_path = args.compare_bt_d23
    if d23_path is None:
        default = ROOT / "results/setup_scanner_runs/bt_d23_pilot_sep11_24h_v1/setups.json"
        if default.is_file():
            d23_path = default

    result = scan_dynamic_upper_pool_setups(
        cfg,
        run_id=args.run_id,
        bt_d23_setups_path=d23_path,
    )
    doc = result_to_json_dict(result)
    (out_dir / "setups.json").write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "candidate_transitions.json").write_text(
        json.dumps(doc["candidate_transitions"], indent=2), encoding="utf-8"
    )
    overlay = setups_document_to_overlay_bundle(doc, symbol=doc.get("symbol"))
    (out_dir / "chart_overlay_bundle.json").write_text(
        json.dumps(overlay, indent=2, sort_keys=True), encoding="utf-8"
    )
    (out_dir / "config_path.txt").write_text(str(args.config.resolve()), encoding="utf-8")
    if not args.no_csv:
        _write_csv(out_dir / "setups.csv", doc["setups"])

    print(f"setup_count={doc['setup_count']} run_dir={out_dir}")
    if doc.get("comparison_bt_d23"):
        print(f"comparison={json.dumps(doc['comparison_bt_d23'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
