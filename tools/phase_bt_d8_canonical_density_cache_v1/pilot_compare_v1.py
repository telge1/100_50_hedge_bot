"""Compare BT-D7 research runs: canonical_provider vs validated_cache."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
for p in (str(BT_D6), str(BT_D7), str(ROOT / "tools/phase4d_pool_landscape_backtester_v2")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_validate_v1 import load_and_validate_config  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from short_research_engine_v1 import run_short_research_backtest  # noqa: E402


def _compare_runs(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {
        "signals_equal": a["signals"] == b["signals"],
        "orders_equal": a["orders"] == b["orders"],
        "trades_equal": a["trades"] == b["trades"],
        "pnl_net_equal": (a.get("pnl") or {}).get("net_pnl_usdt")
        == (b.get("pnl") or {}).get("net_pnl_usdt"),
        "ok": a["signals"] == b["signals"]
        and a["orders"] == b["orders"]
        and a["trades"] == b["trades"]
        and (a.get("pnl") or {}) == (b.get("pnl") or {}),
    }


def run_pilot_compare(provider_config_path: Path, cache_bundle_dir: Path) -> dict[str, Any]:
    cfg_provider = load_and_validate_config(provider_config_path)
    cfg_cache = deepcopy(cfg_provider)
    cfg_cache["density"]["density_source"] = "validated_cache"
    cfg_cache["density"]["validated_cache"] = {"bundle_path": str(cache_bundle_dir.resolve())}

    prices = load_prices_from_ssot_window(
        cfg_provider["backtest"]["start_utc"],
        cfg_provider["backtest"]["end_utc"],
    )
    if not prices:
        return {"blocked": True, "blockers": ["NO_OHLC_IN_SSOT_WINDOW"]}

    r_provider = run_short_research_backtest(
        cfg_provider,
        create_density_session(cfg_provider),
        prices,
    )
    r_cache = run_short_research_backtest(
        cfg_cache,
        create_density_session(cfg_cache),
        prices,
    )
    cmp = _compare_runs(r_provider, r_cache)
    return {
        "compare": cmp,
        "provider": {
            "signals": len(r_provider["signals"]),
            "trades": len(r_provider["trades"]),
            "snapshots": r_provider["snapshots_collected"],
        },
        "cache": {
            "signals": len(r_cache["signals"]),
            "trades": len(r_cache["trades"]),
            "snapshots": r_cache["snapshots_collected"],
        },
        "verdict": "DENSITY_CACHE_PARITY_PASS" if cmp["ok"] else "DENSITY_CACHE_PARITY_BLOCKED",
    }
