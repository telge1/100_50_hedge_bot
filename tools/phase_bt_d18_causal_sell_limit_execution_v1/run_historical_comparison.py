#!/usr/bin/env python3
"""BT-D18: 24h pilot A/B/C × legacy vs causal (no latest_result publish)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
BT_D12 = ROOT / "tools/phase_bt_d12_simple_config_latest_json_overlay_v1"
P4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
for p in (str(BT_D6), str(BT_D7), str(BT_D8), str(BT_D12), str(P4D)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from short_research_engine_v1 import run_short_research_backtest  # noqa: E402

OUT = ROOT / "results/phase_bt_d18_causal_sell_limit_execution_v1"
PILOTS = ROOT / "configs/pilot_bt_d17"


def _summary(result: dict) -> dict:
    fills = [o for o in result.get("orders", []) if o.get("status") == "FILLED"]
    return {
        "signals": len(result.get("signals", [])),
        "orders": len(result.get("orders", [])),
        "fills": len(fills),
        "trades": len(result.get("trades", [])),
        "net_pnl_usdt": (result.get("pnl") or {}).get("net_pnl_usdt"),
        "fill_decisions": [
            {
                "signal_close": f.get("signal_close"),
                "fill_utc": f.get("fill_utc"),
                "fill_price": f.get("fill_price"),
                "rule": f.get("fill_rule_id"),
                "ohlc": f.get("ohlc_evidence"),
            }
            for f in fills
        ],
        "trades": result.get("trades", []),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    variants = [
        "a_pool_lower_edge.yaml",
        "b_pool_upper_edge.yaml",
        "c_pool_lower_edge_offset_0p1.yaml",
    ]
    report_rows: list[dict] = []
    for name in variants:
        base_cfg = load_yaml_and_validate(PILOTS / name)
        prices = load_prices_from_ssot_window(
            base_cfg["backtest"]["start_utc"],
            base_cfg["backtest"]["end_utc"],
            end_utc_exclusive=bool(base_cfg["backtest"].get("end_utc_exclusive")),
        )
        for model in ("legacy_v1", "causal_sell_limit_v2"):
            cfg = copy.deepcopy(base_cfg)
            cfg["short_entry"]["pending_limit_fill_model"] = model
            out = run_short_research_backtest(cfg, create_density_session(cfg), prices)
            tag = f"{name.replace('.yaml', '')}_{model}"
            path = OUT / f"comparison_{tag}.json"
            payload = {"variant": name, "fill_model": model, "summary": _summary(out), "orders": out["orders"]}
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            report_rows.append(payload["summary"] | {"variant": name, "fill_model": model, "artifact": str(path)})
    (OUT / "comparison_index.json").write_text(json.dumps(report_rows, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": "OK", "rows": report_rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
