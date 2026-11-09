"""CLI: catalog inventory + multi-combo dry-run (no ClickHouse writes)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_DASHBOARD = Path(__file__).resolve().parents[3]
_TRP_REPO = _DASHBOARD.parents[2]
for p in (_TRP_REPO, _DASHBOARD):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_inventory import (
    inventory_from_coverage,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    multi_combination_dry_run,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4A historical batch prep (read-only)")
    parser.add_argument("--catalog-root", type=Path, default=CATALOG_ROOT)
    parser.add_argument(
        "--command",
        choices=("inventory", "dry-run"),
        default="inventory",
    )
    parser.add_argument("--samples-per-combo", type=int, default=2)
    args = parser.parse_args(argv)
    root = args.catalog_root.resolve()
    if args.command == "inventory":
        print(json.dumps(inventory_from_coverage(root), indent=2))
        return 0
    body = multi_combination_dry_run(root, samples_per_combo=args.samples_per_combo)
    print(json.dumps(body, indent=2, default=str))
    return 1 if body.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
