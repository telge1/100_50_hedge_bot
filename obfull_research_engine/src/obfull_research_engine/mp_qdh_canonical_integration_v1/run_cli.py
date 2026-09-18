"""CLI: dry-run / pilot for mp_qdh_canonical_integration_v1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MP ↔ Canonical QDH six-event pilot")
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Run output directory (must not already exist with content)",
    )
    p.add_argument("--repo-root", type=Path, default=None)
    p.add_argument("--dry-run-only", action="store_true")
    p.add_argument("--skip-dry-run", action="store_true")
    args = p.parse_args(argv)

    repo = args.repo_root or Path(__file__).resolve().parents[4]
    out = args.out_dir or (
        repo / "obfull_research_engine/runs/mp_qdh_canonical_pilot_v1_20260917"
    )
    from .pilot import run_pilot

    result = run_pilot(
        out_dir=out,
        repo_root=repo,
        dry_run_only=bool(args.dry_run_only),
        skip_dry_run=bool(args.skip_dry_run),
    )
    print(result.get("verdict"), result.get("out_dir"))
    return 0 if result.get("ok") or result.get("verdict") == "DRY_RUN_ONLY" else 1


if __name__ == "__main__":
    sys.exit(main())
