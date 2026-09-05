"""CLI for AGGRESSOR_EFFICIENCY_TRAPPED_VWAP_ACCEPTANCE_V1 smoke."""

from __future__ import annotations

import argparse
from pathlib import Path

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.runner import (
    DEFAULT_OUT,
    run_smoke,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AEF Trapped VWAP + Acceptance V1 (research smoke)")
    p.add_argument("--smoke", action="store_true", help="Run small BTC/DOGE + synthetic smoke only")
    p.add_argument("--skip-ch", action="store_true", help="Synthetic fixtures only")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)
    if not args.smoke and not args.skip_ch:
        # default to smoke for safety
        args.smoke = True
    summary = run_smoke(output_dir=args.output_dir, skip_ch=args.skip_ch)
    print("TRAP_ACCEPT_SMOKE_OK", summary["n_events"], "events", "parity", summary["prefix_parity_ok"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
