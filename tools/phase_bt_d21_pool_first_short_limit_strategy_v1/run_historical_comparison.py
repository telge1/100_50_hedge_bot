#!/usr/bin/env python3
"""BT-D21: baseline proximity vs pool-first on BT-D9 cache (no latest publish)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
):
    sys.path.insert(0, str(p))

from config_validate_v1 import load_and_validate_config
from config_yaml_v1 import load_yaml_and_validate
from density_bridge_v1 import create_density_session
from price_feed_v1 import load_prices_from_ssot_window
from short_research_engine_v1 import run_short_research_backtest

OUT = ROOT / "results/phase_bt_d21_pool_first_short_limit_strategy_v1"


def _summary(label: str, cfg: dict, result: dict) -> dict:
    fills = [o for o in result.get("orders", []) if o.get("status") == "FILLED"]
    first_sig = result["signals"][0] if result.get("signals") else None
    first_fill = fills[0] if fills else None
    trade = result["trades"][0] if result.get("trades") else None
    return {
        "mode": label,
        "signal_count": len(result.get("signals", [])),
        "fill_count": len(fills),
        "trade_count": len(result.get("trades", [])),
        "net_pnl_usdt": (result.get("pnl") or {}).get("net_pnl_usdt"),
        "earliest_signal_close": first_sig["decision_close_utc"] if first_sig else None,
        "earliest_order_active": first_sig["order_active_from_utc"] if first_sig else None,
        "first_fill_utc": first_fill.get("fill_utc") if first_fill else None,
        "first_fill_price": first_fill.get("fill_price") if first_fill else None,
        "first_trade_entry": trade.get("entry_price") if trade else None,
        "first_trade_exit": trade.get("exit_price") if trade else None,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    base = load_and_validate_config(ROOT / "tools/phase_bt_d9_first_24h_short_pnl_v1/configs/bt_d9_24h_v1.json")
    pool_first = load_yaml_and_validate(ROOT / "configs/pilot_bt_d21_pool_first_24h.yaml")
    prices = load_prices_from_ssot_window(
        base["backtest"]["start_utc"],
        base["backtest"]["end_utc"],
        end_utc_exclusive=True,
    )
    session = create_density_session(base)
    r_a = run_short_research_backtest(base, session, prices)
    r_b = run_short_research_backtest(pool_first, create_density_session(pool_first), prices)
    report = {
        "baseline_nearest_proximity": _summary("A_baseline", base, r_a),
        "pool_first_structural": _summary("B_pool_first", pool_first, r_b),
    }
    (OUT / "historical_comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
