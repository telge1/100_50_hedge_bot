#!/usr/bin/env python3
"""BT-D6 foundation smoke (config + synthetic PnL only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
sys.path.insert(0, str(TOOLS))

from config_validate_v1 import load_and_validate_config  # noqa: E402
from experiment_v1 import prepare_experiment  # noqa: E402
from manifest_v1 import build_run_manifest, write_manifest  # noqa: E402
from pnl_aggregator_v1 import SyntheticTrade, aggregate_pnl  # noqa: E402

BASELINE = TOOLS / "configs/pool_landscape_baseline_v1.json"
OUT = ROOT / "results/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_and_validate_config(BASELINE)
    trades = [
        SyntheticTrade("SYN_01", "DOGEUSDT", "SHORT", 0.10, 0.095, "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "TP", 12),
        SyntheticTrade("SYN_02", "DOGEUSDT", "SHORT", 0.10, 0.102, "2026-01-02T00:00:00Z", "2026-01-02T01:00:00Z", "SL", 12),
    ]
    pnl = aggregate_pnl(cfg, trades)
    exp_dir = prepare_experiment(cfg, "baseline_stub", source_config_path=BASELINE)
    manifest = build_run_manifest(
        cfg,
        run_kind="bt_d6_foundation_smoke",
        config_path=str(BASELINE),
        extra={"synthetic_pnl_summary": {k: pnl[k] for k in pnl if k != "trade_ledger" and k != "equity_curve"}},
    )
    write_manifest(OUT / "foundation_run_manifest.json", manifest)
    (OUT / "synthetic_pnl_summary.json").write_text(json.dumps(pnl, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": "CONFIG_PNL_FOUNDATION_PASS",
                "config_hash": manifest["config_hash"],
                "experiment_dir": str(exp_dir),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
