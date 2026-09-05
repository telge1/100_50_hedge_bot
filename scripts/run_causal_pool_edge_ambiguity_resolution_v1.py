#!/usr/bin/env python3
"""CLI: CAUSAL_POOL_EDGE_AMBIGUITY_RESOLUTION_V1 smoke."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_ambiguity_runner import (
    DEFAULT_OUT,
    run_ambiguity_resolution_smoke,
)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    if not args.smoke:
        p.error("only --smoke supported in v1")
    summary = run_ambiguity_resolution_smoke(output_dir=args.output_dir)
    # VERDICT.md stub; full report filled after smoke by agent / optional expand
    verdict = summary.get("verdict_hint", "CAUSAL_POOL_EDGE_AMBIGUITY_RESOLUTION_V1_PARTIAL")
    lines = [
        f"# {verdict}",
        "",
        "outcome_used_for_matching = false",
        "",
        f"- events: {summary.get('n_aef_events')}",
        f"- prior ambiguous resolved: {summary.get('prior_ambiguous_resolved')} / {summary.get('prior_ambiguous_count')}",
        f"- still ambiguous: {summary.get('prior_ambiguous_still_ambiguous')}",
        f"- not reached: {summary.get('prior_ambiguous_not_reached')}",
        f"- HIGH after: {summary.get('n_high')}  MEDIUM after: {summary.get('n_medium')}",
        f"- acceptance events: {summary.get('real_acceptance_events')}",
        f"- prefix_parity_ok: {summary.get('prefix_parity_ok')}",
        f"- elapsed_s: {summary.get('elapsed_s')}  queries: {summary.get('query_count')}",
        "",
        "See SUMMARY.json and ambiguity_resolution_results.csv for full detail.",
        "",
    ]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "VERDICT.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        f"AMBIGUITY_SMOKE {verdict} events {summary.get('n_aef_events')} "
        f"resolved_prior {summary.get('prior_ambiguous_resolved')} "
        f"HIGH {summary.get('n_high')} MEDIUM {summary.get('n_medium')}"
    )
    print(json.dumps({k: summary[k] for k in ("confidence_after", "join_status_after", "acceptance_after") if k in summary}))


if __name__ == "__main__":
    main()
