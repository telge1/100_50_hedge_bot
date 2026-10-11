"""CLI entry for historical JSON import dry-run (no ClickHouse writes)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

_DASHBOARD = Path(__file__).resolve().parents[3]
_TRP_REPO = _DASHBOARD.parents[2]
for p in (_TRP_REPO, _DASHBOARD):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
    PILOT_DECISION_CLOSE_STEMS,
    PILOT_PIPELINE_LINE_ID,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.runner import ImportMode, run_import_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Historical LMV2 JSON → CH dry-run")
    parser.add_argument(
        "--mode",
        choices=("validate", "pilot_write"),
        default="validate",
        help="validate = dry-run only; pilot_write requires env gate (not for Phase 2)",
    )
    parser.add_argument("--catalog-root", type=Path, default=CATALOG_ROOT)
    parser.add_argument("--pipeline-line-id", default=PILOT_PIPELINE_LINE_ID)
    parser.add_argument(
        "--stems",
        nargs="*",
        default=list(PILOT_DECISION_CLOSE_STEMS),
        help="Decision-close filename stems (UTC)",
    )
    parser.add_argument("--test-run-id", default=str(uuid.uuid4()))
    args = parser.parse_args(argv)

    mode = ImportMode.PILOT_WRITE if args.mode == "pilot_write" else ImportMode.VALIDATE
    report = run_import_plan(
        args.stems,
        catalog_root=args.catalog_root.resolve(),
        pipeline_line_id=args.pipeline_line_id,
        mode=mode,
        test_run_id=args.test_run_id,
    )
    print(json.dumps(report, indent=2, default=str))
    return 1 if report.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
