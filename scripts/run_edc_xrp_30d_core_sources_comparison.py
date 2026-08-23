#!/usr/bin/env python3
"""Run XRP 30d core-sources comparison (research-only)."""

from __future__ import annotations


def main() -> int:
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.xrp_30d_core_sources_comparison_runner import (
        run_xrp_30d_core_sources_comparison,
    )

    result = run_xrp_30d_core_sources_comparison()
    print("export_dir:", result["export_dir"])
    print("verdict:", result.get("verdict"))
    print("n_candidates:", result["summary"].get("n_candidates"))
    v = result.get("verdict")
    return 0 if v == "XRP_30D_CORE_SOURCES_COMPARISON_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
