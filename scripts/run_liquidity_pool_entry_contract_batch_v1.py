#!/usr/bin/env python3
"""Entry Contract expansion batch v1 — verify/plan/smoke/resume/status/unblind-gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.hashes import (
    FrozenInputHashMismatch,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v1.runner import (
    BatchError,
    cmd_plan,
    cmd_resume,
    cmd_smoke,
    cmd_status,
    cmd_unblind_outcomes,
    cmd_verify,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--verify", action="store_true")
    g.add_argument("--plan", action="store_true")
    g.add_argument("--smoke", action="store_true")
    g.add_argument("--resume", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--run-all", action="store_true", help="Not allowed in this task")
    g.add_argument("--unblind-outcomes", action="store_true")
    ap.add_argument(
        "--mechanical-only",
        action="store_true",
        help="Required with --smoke; forbids outcome/unblind phase",
    )
    args = ap.parse_args(argv)

    try:
        if args.run_all:
            raise BatchError(
                "EXPANSION_BATCH_V1_SMOKE_FAILED",
                "--run-all forbidden in this task; use --smoke only",
            )
        if args.verify:
            res = cmd_verify(OA_ROOT)
        elif args.plan:
            res = cmd_plan(OA_ROOT)
        elif args.smoke:
            if not args.mechanical_only:
                raise BatchError(
                    "EXPANSION_BATCH_V1_SMOKE_FAILED",
                    "--smoke requires --mechanical-only",
                )
            res = cmd_smoke(OA_ROOT, mechanical_only=True)
        elif args.resume:
            res = cmd_resume(OA_ROOT)
        elif args.status:
            res = cmd_status(OA_ROOT)
        elif args.unblind_outcomes:
            res = cmd_unblind_outcomes(OA_ROOT)
        else:
            ap.error("no mode")
            return 2
    except FrozenInputHashMismatch as e:
        print(json.dumps({"ok": False, "verdict": e.verdict, "detail": e.detail}, indent=2))
        return 2
    except BatchError as e:
        print(json.dumps({"ok": False, "verdict": e.verdict, "detail": str(e)}, indent=2))
        # unblind blocked is expected fail-closed
        if e.verdict in (
            "BATCH_UNBLIND_BLOCKED",
            "BATCH_UNBLIND_NOT_IMPLEMENTED_IN_THIS_TASK",
            "BATCH_MECHANICAL_UNBLIND_SEPARATION_BLOCKED",
        ):
            return 3
        return 2

    print(json.dumps(res, indent=2, default=str))
    # smoke blocked is a defined STOP verdict — non-zero for visibility but structured
    if isinstance(res, dict) and res.get("verdict") == "BATCH_MECHANICAL_UNBLIND_SEPARATION_BLOCKED":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
