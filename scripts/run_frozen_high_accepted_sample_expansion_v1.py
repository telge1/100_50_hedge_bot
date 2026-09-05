#!/usr/bin/env python3
"""CLI: FROZEN_HIGH_ACCEPTED_SAMPLE_EXPANSION_V1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.sample_expansion_runner import (
    DEFAULT_OUT,
    FrozenBundleTampered,
    PRIOR_FREEZE_DIR,
    run_sample_expansion,
)


def _write_abschluss(out: Path, summary: dict) -> None:
    lines = [
        f"# {summary.get('verdict')}",
        "",
        "rein deskriptiv · kein Trading-Edge bewiesen · keine Entry-/Exit-Optimierung",
        "",
        "```",
        "outcome_used_for_matching = false",
        "outcome_used_for_thresholds = false",
        "outcome_used_for_state_definition = false",
        "outcome_used_for_sample_selection = false",
        "```",
        "",
        f"- stop_reason: {summary.get('stop_reason')}",
        f"- HIGH∩ACCEPTED_ANY: {summary.get('n_high_accepted_any')}",
        f"- HIGH∩ACCEPTED_ABOVE: {summary.get('n_high_accepted_above')}",
        f"- HIGH∩ACCEPTED_BELOW: {summary.get('n_high_accepted_below')}",
        f"- hours_processed: {summary.get('n_hours_processed')}",
        f"- ready: {summary.get('ready')}",
        f"- freeze before/after: {summary.get('freeze_bundle_sha256_before')} / {summary.get('freeze_bundle_sha256_after')}",
        f"- elapsed_s: {summary.get('elapsed_s')}  queries: {summary.get('query_count')}",
        "",
        "Siehe ABSCHLUSSBERICHT.md für den vollständigen Bericht.",
        "",
    ]
    # stub if full report not yet written
    p = out / "ABSCHLUSSBERICHT.md"
    if not p.exists():
        p.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--freeze-dir", type=Path, default=PRIOR_FREEZE_DIR)
    p.add_argument("--target-n", type=int, default=30)
    p.add_argument("--max-hours", type=int, default=None, help="optional cap for smoke tests")
    args = p.parse_args()
    try:
        summary = run_sample_expansion(
            output_dir=args.output_dir,
            freeze_dir=args.freeze_dir,
            target_n=args.target_n,
            max_hours=args.max_hours,
        )
    except FrozenBundleTampered as e:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {"verdict": "FROZEN_BUNDLE_TAMPERED", "error": str(e)}
        (args.output_dir / "verdict.json").write_text(json.dumps(payload, indent=2) + "\n")
        print("FROZEN_BUNDLE_TAMPERED", e)
        raise SystemExit(2)
    _write_abschluss(args.output_dir, summary)
    print(
        f"EXPANSION {summary.get('verdict')} stop={summary.get('stop_reason')} "
        f"HIGH∩ACCEPTED_ANY={summary.get('n_high_accepted_any')} "
        f"hours={summary.get('n_hours_processed')}"
    )


if __name__ == "__main__":
    main()
