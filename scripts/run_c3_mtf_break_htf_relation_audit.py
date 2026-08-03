#!/usr/bin/env python3
"""CLI: Causal 5m break ↔ HTF relation audit (read-only artefacts)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
src = ROOT / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

from orderbook_analyse.c3_mtf_break_htf_relation_audit import (  # noqa: E402
    run_mtf_break_htf_relation_audit,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="C3 multi-event causal MTF break ↔ HTF relation audit"
    )
    p.add_argument(
        "--symbols",
        default="APTUSDT,DOGEUSDT,BTCUSDT",
        help="Comma-separated symbols",
    )
    p.add_argument(
        "--mtf-dir",
        type=Path,
        default=ROOT / "results" / "trend_scanner_multitimeframe_structure",
    )
    p.add_argument(
        "--pl-catalog-dir",
        type=Path,
        default=ROOT / "results" / "c3_protected_low_historical_event_catalog",
    )
    p.add_argument(
        "--ph-catalog-dir",
        type=Path,
        default=ROOT / "results" / "c3_protected_high_historical_event_catalog",
    )
    p.add_argument(
        "--pl-deep-dive-dir",
        type=Path,
        default=ROOT / "results" / "c3_protected_low_event_driven_decision_deep_dive",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "results" / "c3_mtf_break_htf_relation_audit",
    )
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    symbols = tuple(s.strip() for s in str(args.symbols).split(",") if s.strip())
    result = run_mtf_break_htf_relation_audit(
        mtf_dir=args.mtf_dir,
        pl_catalog_dir=args.pl_catalog_dir,
        ph_catalog_dir=args.ph_catalog_dir,
        pl_deep_dive_dir=args.pl_deep_dive_dir,
        output_dir=args.output_dir,
        symbols=symbols,
        overwrite=bool(args.overwrite),
    )
    decision = result["decision"]
    print(
        json.dumps(
            {
                "primary_decision": decision["primary_decision"],
                "rationale": decision.get("rationale"),
                "n_events": decision.get("n_events"),
                "n_outcomes": decision.get("n_outcomes"),
                "lookahead_pass": result["lookahead"]["pass"],
            },
            indent=2,
        )
    )
    print(f"Wrote artefacts to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
