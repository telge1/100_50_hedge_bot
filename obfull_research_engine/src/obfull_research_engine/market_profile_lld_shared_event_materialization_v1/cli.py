"""CLI for the three-timestamp pilot only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MP/LLD shared event materialization v1 (pilot)")
    parser.add_argument("--pilot", action="store_true", help="run the three Phase-1 timestamps")
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args(argv)
    if not args.pilot:
        parser.error("this phase only supports --pilot")
    sys.path[:0] = [
        str(Path(__file__).resolve().parents[2]),
        str(Path("/home/telgenbuescher/projects/orderbook_analyse/src")),
        "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard",
    ]
    from .runner import run_pilot

    result = run_pilot(resume=not args.no_resume)
    print(json.dumps({k: result[k] for k in result if k != "manifest"}, indent=2, default=str))
    return 0 if result.get("status") == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
