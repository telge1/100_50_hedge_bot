"""CLI entry for first-touch study."""

from __future__ import annotations

import argparse
from pathlib import Path

from .run_smoke import run_smoke
from .run_study import run_study


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mp_qdh_first_touch_study_v1")
    p.add_argument("--repo-root", type=Path, default=None)
    p.add_argument("--smoke-only", action="store_true")
    p.add_argument("--skip-smoke", action="store_true")
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--smoke-out-dir", type=Path, default=None)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--max-events", type=int, default=None)
    args = p.parse_args(argv)
    if args.smoke_only:
        r = run_smoke(repo_root=args.repo_root, out_dir=args.smoke_out_dir or args.out_dir)
        print(r.get("verdict"), r.get("out_dir"))
        return 0 if r.get("ok") else 1
    r = run_study(
        repo_root=args.repo_root,
        out_dir=args.out_dir,
        smoke_out=args.smoke_out_dir,
        require_smoke=not args.skip_smoke,
        resume=not args.no_resume,
        max_events=args.max_events,
    )
    print(r.get("verdict"), r.get("out_dir"))
    return 0 if r.get("verdict") in (
        "FIRST_TOUCH_STUDY_SUCCESS",
        "FIRST_TOUCH_STUDY_PARTIAL",
        "FIRST_TOUCH_SMOKE_SUCCESS",
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
