#!/usr/bin/env python3
"""Strict 1:1 ORIGINAL_XRP_STRATEGY vs MULTICOIN_BACKTESTER_XRP_REPLAY audit.

Writes only under results/edc_sync_tolerance/diagnostics/xrp_strategy_parity/
Does not overwrite multicoin frozen validation or original XRP SoT artifacts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.xrp_strategy_parity.audit import (  # noqa: E402
    run_audit,
)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    skip = "--skip-replay" in argv
    out = run_audit(skip_replay=skip)
    print("verdict:", out.get("verdict"))
    print("audit_dir: results/edc_sync_tolerance/diagnostics/xrp_strategy_parity/")
    if out.get("verdict") == "XRP_SOURCE_OF_TRUTH_AMBIGUOUS":
        return 2
    if out.get("verdict") == "MULTICOIN_BACKTESTER_USES_XRP_STRATEGY_1_TO_1_CONFIRMED":
        return 0
    # Print compact mismatch lead
    print("static_mismatches:", out.get("static_mismatches"))
    cp = out.get("candidate_parity") or {}
    tp = out.get("trade_parity") or {}
    print(
        "candidates:",
        f"exact={cp.get('n_exact_match')}/{cp.get('n_common')}",
        f"only_o={len(cp.get('only_original') or [])}",
        f"only_r={len(cp.get('only_replay') or [])}",
    )
    print(
        "trades:",
        f"exact={tp.get('n_exact_trade_match')}/{tp.get('n_common')}",
        f"only_o={len(tp.get('only_original') or [])}",
        f"only_r={len(tp.get('only_replay') or [])}",
        f"net_pnl_mismatches={tp.get('net_pnl_mismatch_count')}",
    )
    print("original_net_pnl:", (out.get("original_metrics") or {}).get("net_pnl_usdt"))
    print("replay_net_pnl:", (out.get("replay_metrics") or {}).get("net_pnl_usdt"))
    for m in (out.get("first_20_mismatches") or [])[:10]:
        print(" mismatch:", m.get("candidate_id"), m.get("diffs"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
