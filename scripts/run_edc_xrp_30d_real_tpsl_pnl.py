#!/usr/bin/env python3
"""Run XRP 30d real TP/SL PnL backtest."""

from __future__ import annotations


def main() -> int:
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.xrp_30d_real_tpsl_pnl_runner import (
        run_xrp_30d_real_tpsl_pnl,
    )

    result = run_xrp_30d_real_tpsl_pnl()
    print("export_dir:", result["export_dir"])
    print("verdict:", result.get("verdict"))
    v = result.get("verdict")
    return 0 if v == "XRP_30D_REAL_TPSL_PNL_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
