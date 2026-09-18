"""CLI for mp_qdh_wall_linkage_audit_v1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--repo-root", type=Path, default=None)
    p.add_argument("--dry-run-only", action="store_true")
    args = p.parse_args(argv)
    repo = args.repo_root or Path(__file__).resolve().parents[4]
    out = args.out_dir or (repo / "obfull_research_engine/runs/mp_qdh_wall_linkage_audit_v1_20260917")
    from .run_audit import run_audit

    result = run_audit(out_dir=out, repo_root=repo, dry_run_only=bool(args.dry_run_only))
    print(result.get("verdict"), result.get("out_dir"))
    return 0 if result.get("ok") or result.get("verdict") == "DRY_RUN_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
