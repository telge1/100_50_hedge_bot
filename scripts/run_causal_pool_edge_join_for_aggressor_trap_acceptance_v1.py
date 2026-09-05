#!/usr/bin/env python3
"""Causal pool-edge join for AEF trap/acceptance stage-1 smoke."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_join_runner import (  # noqa: E402
    DEFAULT_OUT,
    run_causal_edge_join_smoke,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--smoke", action="store_true", default=True)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)
    summary = run_causal_edge_join_smoke(output_dir=args.output_dir)
    print(
        "EDGE_JOIN_SMOKE",
        summary.get("verdict_hint"),
        "events",
        summary.get("n_aef_events"),
        "HIGH",
        (summary.get("confidence") or {}).get("HIGH"),
        "MEDIUM",
        (summary.get("confidence") or {}).get("MEDIUM"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
