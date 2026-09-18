"""CLI for mp_qdh_30event_case_control_v2."""

from __future__ import annotations

import argparse
from pathlib import Path

from .run_study import run_study


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mp_qdh_30event_case_control_v2")
    p.add_argument("--repo-root", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--dry-run-only", action="store_true")
    p.add_argument("--dry-run-pairs", type=int, default=1)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--max-pairs", type=int, default=None)
    args = p.parse_args(argv)
    result = run_study(
        out_dir=args.out_dir,
        repo_root=args.repo_root,
        dry_run_only=bool(args.dry_run_only),
        dry_run_pairs=int(args.dry_run_pairs),
        resume=not bool(args.no_resume),
        max_pairs=args.max_pairs,
    )
    print(result.get("verdict"), result.get("out_dir"))
    v = result.get("verdict")
    return 0 if v in (
        "QDH_30EVENT_V2_SUCCESS",
        "QDH_30EVENT_V2_PARTIAL",
        "DRY_RUN_ONLY",
        "EVENT_UNIVERSE_MISMATCH",
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
