#!/usr/bin/env python3
"""Entry Contract expansion batch v2 — verify/plan/smoke/resume/status/unblind-gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.hashes import (
    FrozenInputHashMismatch,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.runner import (
    BatchError,
    cmd_plan,
    cmd_resume,
    cmd_smoke,
    cmd_status,
    cmd_unblind_outcomes,
    cmd_verify,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.partial_runner import (
    cmd_partial_12,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.partial_sample import (
    PartialSampleError,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.coordination import (
    CoordinationError,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--verify", action="store_true")
    g.add_argument("--plan", action="store_true")
    g.add_argument("--smoke", action="store_true")
    g.add_argument("--partial-12", action="store_true", help="Mechanical 12/24 partial sample")
    g.add_argument("--resume", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--run-all", action="store_true", help="Not allowed in this task")
    g.add_argument("--unblind-outcomes", action="store_true")
    ap.add_argument(
        "--mechanical-only",
        action="store_true",
        help="Required with --smoke/--partial-12; forbids outcome/unblind phase",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Max workers for --partial-12 (1 or 2; default 2)",
    )
    ap.add_argument(
        "--raw-root",
        type=Path,
        default=None,
        help="Raw OB200 root (default: data/orderbook_raw_shadow/ob200_v3)",
    )
    args = ap.parse_args(argv)

    try:
        if args.run_all:
            raise BatchError(
                "EXPANSION_BATCH_V2_SMOKE_FAILED",
                "--run-all forbidden in this task; use --smoke or --partial-12",
            )
        if args.verify:
            res = cmd_verify(OA_ROOT)
        elif args.plan:
            res = cmd_plan(OA_ROOT)
        elif args.smoke:
            if not args.mechanical_only:
                raise BatchError(
                    "EXPANSION_BATCH_V2_SMOKE_FAILED",
                    "--smoke requires --mechanical-only",
                )
            res = cmd_smoke(OA_ROOT, mechanical_only=True, raw_root=args.raw_root)
        elif args.partial_12:
            if not args.mechanical_only:
                raise BatchError(
                    "EXPANSION_BATCH_V2_PARTIAL_RETRYABLE",
                    "--partial-12 requires --mechanical-only",
                )
            res = cmd_partial_12(
                OA_ROOT,
                mechanical_only=True,
                raw_root=args.raw_root,
                concurrency=args.concurrency,
            )
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
    except (BatchError, PartialSampleError, CoordinationError) as e:
        verdict = getattr(e, "verdict", "EXPANSION_BATCH_V2_PARTIAL_RETRYABLE")
        print(json.dumps({"ok": False, "verdict": verdict, "detail": str(e)}, indent=2))
        if verdict in (
            "MECHANICAL_UNBLIND_SEPARATION_FAILURE",
            "SMOKE_PREFIX_PARITY_FAILURE",
            "BATCH_PREFIX_PARITY_FAILURE",
            "FROZEN_INPUT_HASH_MISMATCH",
            "EXP_ASK_REAL_DATA_EXECUTION_BLOCKED",
            "EXP_BID_REAL_DATA_EXECUTION_BLOCKED",
            "PARALLEL_BATCH_COORDINATION_FAILURE",
            "OUTCOME_BLINDNESS_VIOLATION",
            "EXPANSION_BATCH_V2_PARTIAL_12_MECHANICAL_COMPLETE",
        ):
            return 3
        return 2

    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
