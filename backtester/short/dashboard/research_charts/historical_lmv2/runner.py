"""CLI entry for historical LMV2 short baseline research (no full backtests in phase 1)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .catalog import discover_production_catalog
from .variants import BASELINE_VARIANTS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Historical LMV2 Floor-Guard Short Baseline research runner")
    parser.add_argument(
        "--production-root",
        type=Path,
        required=True,
        help="e.g. doge_multi_month_2026_history (contains production/)",
    )
    parser.add_argument("--list-catalog", action="store_true", help="Print manifest gate summary and exit")
    parser.add_argument("--list-variants", action="store_true", help="Print six PD/guard variants")
    args = parser.parse_args(argv)

    if args.list_variants:
        for v in BASELINE_VARIANTS:
            print(json.dumps({"name": v.name, "run_key": v.run_key, "guard": v.floor_guard_enabled}))
        return 0

    catalog = discover_production_catalog(args.production_root.resolve())
    if args.list_catalog:
        print(
            json.dumps(
                {
                    "name": catalog.name,
                    "frozen_input_id": catalog.frozen_input_id,
                    "allowed_months": sorted(catalog.allowed_months()),
                    "releases": [
                        {
                            "month": r.month,
                            "status": r.month_status,
                            "candidate": r.backtester_candidate,
                        }
                        for r in catalog.releases
                    ],
                },
                indent=2,
            )
        )
        return 0

    parser.error("Specify --list-catalog or --list-variants (full walk not enabled in this phase)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
