#!/usr/bin/env python3
"""CLI: FROZEN_HIGH_EDGE_FORWARD_OUTCOME_EVALUATION_V1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.frozen_outcome_runner import (
    DEFAULT_OUT,
    run_frozen_evaluation,
)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--phase-a", action="store_true", help="Freeze + smoke + coverage (default path)")
    p.add_argument(
        "--expand",
        action="store_true",
        help="Also run Phase C bounded expand if coverage plan allows",
    )
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    if not args.phase_a and not args.expand:
        args.phase_a = True
    summary = run_frozen_evaluation(
        output_dir=args.output_dir,
        run_expand=bool(args.expand),
    )
    verdict = summary.get("verdict_hint", "FROZEN_HIGH_EDGE_OUTCOMES_V1_PARTIAL")
    lines = [
        f"# {verdict}",
        "",
        "outcome_used_for_matching = false",
        "outcome_used_for_thresholds = false",
        "outcome_used_for_state_definition = false",
        "",
        f"- phase_a events: {summary.get('n_aef_events')}",
        f"- HIGH: {summary.get('n_high')} ({summary.get('high_sample_size_label')})",
        f"- freeze_bundle_sha256: {summary.get('freeze_bundle_sha256')}",
        f"- expand_allowed: {(summary.get('coverage_expand_plan') or {}).get('allowed')}",
        f"- phase_c_ran: {summary.get('phase_c_ran')}",
        f"- elapsed_s: {summary.get('elapsed_s')}  queries: {summary.get('query_count')}",
        "",
        "See VERDICT.md (full) and SUMMARY.json.",
        "",
    ]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # stub; full VERDICT written by post-pass if needed
    stub = args.output_dir / "VERDICT.md"
    if not stub.exists():
        stub.write_text("\n".join(lines), encoding="utf-8")
    print(f"FROZEN_OUTCOMES {verdict} HIGH {summary.get('n_high')} n {summary.get('n_aef_events')}")
    print(json.dumps({k: summary.get(k) for k in ("confidence", "acceptance", "combined", "high_sample_size_label")}, default=str))


if __name__ == "__main__":
    main()
