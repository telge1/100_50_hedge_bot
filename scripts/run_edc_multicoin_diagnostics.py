#!/usr/bin/env python3
"""Offline diagnostics for frozen multicoin reference results (no backtest)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.diagnostics_analysis import (  # noqa: E402
    run_diagnostics,
)


def main() -> int:
    summary = run_diagnostics()
    print("diagnostics_dir: results/edc_sync_tolerance/multicoin_30d_frozen_validation/diagnostics")
    print("verdict:", summary["verdict"])
    print("n_profitable:", summary["n_profitable"])
    print("profitable:", [p["symbol"] for p in summary["profitable_coins"]])
    print("stable_positive:", summary.get("stable_positive"))
    print("model_status:", summary.get("model_status"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
