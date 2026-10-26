#!/usr/bin/env python3
"""Phase 1E — CLI manual reference case capture (read-only SSOT)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results" / "phase1e_depth2_reference_capture_v1"
REGISTRY = RESULTS / "manual_reference_registry_v1.jsonl"
SEED = Path(__file__).resolve().parent / "registry_seed_v1.jsonl"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_lib import (  # noqa: E402
    CAPTURE_VERSION,
    build_capture_record,
    gaps_flat_from_bundle,
    strip_bundle_for_registry,
)
from run_pilot import write_csv  # noqa: E402


def append_registry(line: dict[str, Any]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    with REGISTRY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, separators=(",", ":")) + "\n")


def write_case_artifacts(record: dict[str, Any]) -> Path:
    case_id = record["case_id"]
    case_dir = RESULTS / "cases" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    (case_dir / "capture_record.json").write_text(
        json.dumps(strip_bundle_for_registry(record), indent=2),
        encoding="utf-8",
    )

    bundle = record.get("landscape_bundle")
    if not bundle:
        (case_dir / "landscape_skipped.json").write_text(
            json.dumps(
                {
                    "reason": record.get("landscape_status"),
                    "timestamp_status": record.get("timestamp_status"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return case_dir

    (case_dir / "landscape_profile.json").write_text(
        json.dumps(bundle, indent=2), encoding="utf-8"
    )
    intervals = bundle.get("depth2_intervals") or []
    write_csv(
        case_dir / "depth2_intervals.csv",
        ["side", "bottom_price", "top_price", "depth", "origins"],
        [
            {
                "side": iv["side"],
                "bottom_price": repr(iv["bottom_price"]),
                "top_price": repr(iv["top_price"]),
                "depth": iv["depth"],
                "origins": "|".join(iv.get("origins") or []),
            }
            for iv in intervals
        ],
    )
    gaps = gaps_flat_from_bundle(bundle)
    write_csv(
        case_dir / "pool_gaps.csv",
        ["side", "gap_bottom_price", "gap_top_price", "gap_pct", "large_gap"],
        [
            {
                "side": g["side"],
                "gap_bottom_price": repr(g["gap_bottom_price"]),
                "gap_top_price": repr(g["gap_top_price"]),
                "gap_pct": repr(g["gap_pct"]),
                "large_gap": g["large_gap"],
            }
            for g in gaps
        ],
    )
    if record.get("entry_geometry"):
        (case_dir / "entry_geometry.json").write_text(
            json.dumps(record["entry_geometry"], indent=2), encoding="utf-8"
        )
    if record.get("ema"):
        (case_dir / "ema_context.json").write_text(
            json.dumps(record["ema"], indent=2), encoding="utf-8"
        )
    return case_dir


def cmd_capture(args: argparse.Namespace) -> int:
    price = args.manual_entry_price
    if price is not None:
        price = float(price)

    record = build_capture_record(
        args.case_id,
        args.decision.upper(),
        args.decision_time_utc,
        price,
        args.user_reason,
        args.screenshot_reference,
        force_ssot=args.force_ssot,
    )
    write_case_artifacts(record)
    append_registry(strip_bundle_for_registry(record))
    print(json.dumps(strip_bundle_for_registry(record), indent=2))
    if record.get("landscape_status") == "COMPLETE" and not record.get("parity_ok"):
        return 2
    return 0


def cmd_init_seed(args: argparse.Namespace) -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    dest = RESULTS / "registry_seed_v1.jsonl"
    dest.write_text(SEED.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Copied seed to {dest}")
    return 0


def cmd_pilot(_args: argparse.Namespace) -> int:
    """Pilot: S5 full profile + N3 timestamp skip only."""
    codes = []
    codes.append(
        cmd_capture(
            argparse.Namespace(
                case_id="S5",
                decision="SHORT",
                decision_time_utc="2026-09-13T02:45:00Z",
                manual_entry_price=0.085071,
                user_reason="Phase 1E pilot — accepted short reference",
                screenshot_reference="image(20261022-083819).png",
                force_ssot=False,
            )
        )
    )
    codes.append(
        cmd_capture(
            argparse.Namespace(
                case_id="N3",
                decision="NO_SHORT",
                decision_time_utc=None,
                manual_entry_price=None,
                user_reason="stacked upper pools — no UTC",
                screenshot_reference="image(20261022-091843).png",
                force_ssot=False,
            )
        )
    )
    summary = {
        "pilot": CAPTURE_VERSION,
        "verdict": "DEPTH2_REFERENCE_CAPTURE_READY"
        if codes[0] == 0 and codes[1] == 0
        else "DEPTH2_REFERENCE_CAPTURE_BLOCKED",
        "s5_exit": codes[0],
        "n3_exit": codes[1],
    }
    (RESULTS / "pilot_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["verdict"] == "DEPTH2_REFERENCE_CAPTURE_READY" else 2


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 1E manual reference capture")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("capture", help="Capture one case")
    c.add_argument("--case-id", required=True)
    c.add_argument(
        "--decision",
        required=True,
        choices=["SHORT", "NO_SHORT", "LONG", "NO_LONG"],
    )
    c.add_argument("--decision-time-utc", default=None)
    c.add_argument("--manual-entry-price", type=float, default=None)
    c.add_argument("--user-reason", default=None)
    c.add_argument("--screenshot-reference", default=None)
    c.add_argument("--force-ssot", action="store_true")
    c.set_defaults(func=cmd_capture)

    i = sub.add_parser("init-seed", help="Copy registry seed into results/")
    i.set_defaults(func=cmd_init_seed)

    pl = sub.add_parser("pilot", help="Run S5 + N3 pilot")
    pl.set_defaults(func=cmd_pilot)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
