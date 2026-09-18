"""CLI for isolated smokes (no live arm)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ema_trend_live_analyzer_v1")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("contract", help="Write CONTRACT_MANIFEST.json")
    sm = sub.add_parser("isolated-smoke", help="Run isolated fake-clock / realtime smoke")
    sm.add_argument("--mode", choices=("fake300", "realtime65"), default="fake300")
    sm.add_argument("--run-dir", type=str, default="")
    args = p.parse_args(argv)

    root = Path(__file__).resolve().parents[3]
    run_dir = Path(args.run_dir) if args.run_dir else root / "runs" / "ema_trend_live_phase3_phase9_v1_20260918"
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.cmd == "contract":
        from .contract import write_contract_manifest

        collector = Path("/home/telgenbuescher/projects/orderbook_analyse_ema_delta_fanout_v1/src/orderbook_analyse")
        man = write_contract_manifest(run_dir / "CONTRACT_MANIFEST.json", collector_src_root=collector)
        print(json.dumps({"ok": True, "contract_hash": man["contract_hash"]}, indent=2))
        return 0

    if args.cmd == "isolated-smoke":
        from .smoke_isolated import run_smoke

        result = run_smoke(mode=args.mode, run_dir=run_dir)
        print(json.dumps({"ok": result["ok"], "status": result["status"], "mode": args.mode}, indent=2))
        return 0 if result["ok"] else 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
