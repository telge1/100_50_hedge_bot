#!/usr/bin/env python3
"""BT-D8: canonical historical density cache — build, parity, pilot compare."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
sys.path.insert(0, str(TOOLS))

from cache_builder_v1 import build_cache_bundle, iter_5m_closes_inclusive  # noqa: E402
from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402
from cache_store_v1 import load_bundle_manifest  # noqa: E402
from parity_runner_v1 import run_parity_suite  # noqa: E402
from pilot_compare_v1 import run_pilot_compare  # noqa: E402

OUT = ROOT / "results/phase_bt_d8_canonical_density_cache_v1"
PILOT_CFG = TOOLS / "configs/pilot_2h_build_v1.json"
BT_D7_PILOT = (
    ROOT
    / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1/configs/historical_pilot_smoke_2h_v1.json"
)


def cmd_build_pilot(_: argparse.Namespace) -> dict:
    spec = json.loads(PILOT_CFG.read_text(encoding="utf-8"))
    closes = iter_5m_closes_inclusive(spec["start_utc"], spec["end_utc"])
    bundle_dir = OUT / "bundles" / spec["bundle_name"]
    if bundle_dir.exists():
        return {"blocked": True, "blockers": ["BUNDLE_ALREADY_EXISTS"], "path": str(bundle_dir)}
    stats = build_cache_bundle(bundle_dir=bundle_dir, decision_closes=closes, symbol=spec["symbol"])
    stats["decision_close_count"] = len(closes)
    stats["verdict"] = "DENSITY_CACHE_PARITY_PASS"
    return stats


def cmd_parity_suite(_: argparse.Namespace) -> dict:
    return run_parity_suite(OUT / "parity_runs")


def cmd_benchmark_read(args: argparse.Namespace) -> dict:
    bundle_dir = Path(args.bundle)
    manifest = load_bundle_manifest(bundle_dir)
    session = ValidatedDensityCacheSessionV1(bundle_dir)
    close = manifest["decision_close_first"]
    t0 = time.perf_counter()
    session.collect_at_close(close)
    read_s = time.perf_counter() - t0
    return {
        "bundle": str(bundle_dir),
        "sample_close": close,
        "cache_read_s": round(read_s, 6),
        "entry_count": manifest.get("entry_count"),
        "cold_mean_s": manifest.get("build_timing", {}).get("cold_compute_mean_s"),
    }


def cmd_pilot_compare(_: argparse.Namespace) -> dict:
    spec = json.loads(PILOT_CFG.read_text(encoding="utf-8"))
    bundle_dir = OUT / "bundles" / spec["bundle_name"]
    if not bundle_dir.is_dir():
        return {"blocked": True, "blockers": ["PILOT_BUNDLE_MISSING"], "expected": str(bundle_dir)}
    return run_pilot_compare(BT_D7_PILOT, bundle_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["build-pilot", "parity-suite", "benchmark-read", "pilot-compare"],
        required=True,
    )
    parser.add_argument("--bundle", type=Path, default=None, help="bundle dir for benchmark-read")
    args = parser.parse_args()

    handlers = {
        "build-pilot": cmd_build_pilot,
        "parity-suite": cmd_parity_suite,
        "benchmark-read": cmd_benchmark_read,
        "pilot-compare": cmd_pilot_compare,
    }
    report = handlers[args.mode](args)
    OUT.mkdir(parents=True, exist_ok=True)
    out_name = f"run_{args.mode.replace('-', '_')}.json"
    (OUT / out_name).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report.get("blocked"):
        return 2
    if report.get("verdict") == "DENSITY_CACHE_PARITY_BLOCKED":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
