#!/usr/bin/env python3
"""UTC-correct research public-trade rematerialization CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.btc_doge_research.clickhouse import connect
from research.btc_doge_research.trade_contract import contract_manifest
from research.btc_doge_research.trade_importer import (
    audit_report,
    build_full_backfill_plan,
    compute_canonical_invariants,
    run_full,
    run_pilot_twice,
    status_report,
)
from research.btc_doge_research.trade_rematerialization import (
    RESULT_ROOT,
    backup_shifted_state,
    build_plan,
    ensure_result_root,
    write_downstream_lineage_audit,
    write_json,
    write_rollback_plan,
)
from research.btc_doge_research.trade_run_state import ensure_run_dirs


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true")
    g.add_argument("--pilot", action="store_true", help="Phase-7 pilots x2 + idempotency")
    g.add_argument("--run", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--audit", action="store_true")
    g.add_argument("--backup-only", action="store_true")
    g.add_argument("--lineage-audit", action="store_true")
    g.add_argument("--invariants", action="store_true")
    p.add_argument("--resume", action="store_true", default=True)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--symbol", choices=("BTCUSDT", "DOGEUSDT"), action="append")
    p.add_argument("--launcher-pid", type=int, default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    ensure_result_root()
    ensure_run_dirs()
    client = connect()
    resume = not args.no_resume
    launcher_pid = args.launcher_pid or os.getppid()

    if args.plan:
        write_downstream_lineage_audit(client)
        write_rollback_plan()
        plan = build_plan(client)
        full = build_full_backfill_plan(client)
        write_json(RESULT_ROOT / "contract_manifest.json", contract_manifest())
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "plan",
                    "approx_unique_trades": plan["approx_unique_trades"],
                    "full_backfill_plan": full,
                },
                indent=2,
                default=str,
            )
        )
        return 0

    if args.lineage_audit:
        out = write_downstream_lineage_audit(client)
        print(json.dumps({"ok": True, "tables": len(out["rows"])}, indent=2))
        return 0

    if args.backup_only:
        manifest = backup_shifted_state(client)
        write_rollback_plan()
        print(json.dumps({"ok": True, "backup_dir": manifest["backup_dir"]}, indent=2))
        return 0

    if args.pilot:
        write_downstream_lineage_audit(client)
        write_rollback_plan()
        build_plan(client)
        out = run_pilot_twice(client)
        print(json.dumps({"ok": out["verdict"] == "PILOT_PASS", **out}, indent=2, default=str))
        return 0 if out["verdict"] == "PILOT_PASS" else 2

    if args.run:
        symbols = tuple(args.symbol) if args.symbol else ("BTCUSDT", "DOGEUSDT")
        # Gate: require pilot idempotency artifact
        idem_path = RESULT_ROOT / "pilot_idempotency.json"
        if not idem_path.is_file():
            print("BLOCKED: run --pilot first", file=sys.stderr)
            return 3
        idem = json.loads(idem_path.read_text())
        if idem.get("verdict") != "IDEMPOTENCY_PASS":
            print(f"BLOCKED: pilot idempotency={idem.get('verdict')}", file=sys.stderr)
            return 3
        build_full_backfill_plan(client)
        out = run_full(client, resume=resume, symbols=symbols, launcher_pid=launcher_pid)
        print(json.dumps({"ok": True, "summary": out}, indent=2, default=str))
        return 0 if out.get("status") in {"COMPLETED", "STOPPED"} else 2

    if args.status:
        out = status_report(client)
        print(json.dumps(out, indent=2, default=str))
        return 0

    if args.audit:
        out = audit_report(client)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out["all_pass"] else 2

    if args.invariants:
        out = compute_canonical_invariants(client)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("pass") else 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
