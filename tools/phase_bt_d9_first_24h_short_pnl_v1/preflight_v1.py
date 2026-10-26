"""BT-D9 fail-closed preflight before long cache build."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D4 = ROOT / "tools/phase_bt_d4_enforced_depth2_backtester_v1"
for p in (str(BT_D6), str(BT_D7), str(BT_D4)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_validate_v1 import load_and_validate_config  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from research_backtester_v1 import ResearchDensityRunV1  # noqa: E402

from window_v1 import iter_5m_closes_half_open  # noqa: E402

REQUIRED_PNL_KEYS = (
    "initial_equity_usdt",
    "sizing_mode",
    "position_notional_usdt",
    "leverage",
    "trading_fee_model",
    "slippage_model",
    "funding_model",
    "intrabar_ambiguity_policy",
    "end_of_test_open_position_policy",
)


def run_preflight(
    config_path: Path,
    *,
    required_closes: list[str],
    require_complete_cache: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {"checks": {}, "blockers": [], "verdict": "BT_D9_24H_PNL_PASS"}
    try:
        cfg = load_and_validate_config(config_path)
        report["checks"]["config_valid"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        report["checks"]["config_valid"] = {"ok": False, "error": str(exc)}
        report["blockers"].append("CONFIG_INVALID")
        report["verdict"] = "BT_D9_24H_PNL_BLOCKED"
        return report

    d = cfg["density"]
    if d.get("method") != "exact_unique_origin" or d.get("min_unique_origins") != 2:
        report["blockers"].append("DENSITY_CONTRACT_VIOLATION")
    if cfg["take_profit"]["selection_mode"] != "next_eligible_band":
        report["blockers"].append("TP_MODE_NOT_EXECUTABLE")
    if cfg["short_entry"].get("upper_gap_min_pct") is None:
        report["blockers"].append("GAP_GATE_MISSING")

    pnl = cfg["pnl"]
    missing_pnl = [k for k in REQUIRED_PNL_KEYS if k not in pnl]
    if missing_pnl:
        report["blockers"].append("PNL_PARAMS_MISSING")
        report["checks"]["pnl_params"] = {"ok": False, "missing": missing_pnl}
    else:
        report["checks"]["pnl_params"] = {"ok": True}

    report["checks"]["intrabar_policy"] = {"value": pnl.get("intrabar_ambiguity_policy")}
    report["checks"]["eot_policy"] = {"value": pnl.get("end_of_test_open_position_policy")}
    report["checks"]["pending_limit"] = {
        "enabled": cfg["short_entry"].get("pending_limit_enabled"),
        "expiry_bars": cfg["short_entry"].get("pending_limit_expiry_bars"),
    }

    bt = cfg["backtest"]
    if not bt.get("end_utc_exclusive"):
        report["blockers"].append("END_UTC_EXCLUSIVE_REQUIRED")
    start, end = bt["start_utc"], bt["end_utc"]
    closes = iter_5m_closes_half_open(start, end)
    if len(closes) != len(required_closes):
        report["blockers"].append("CLOSE_WINDOW_MISMATCH")
    report["checks"]["decision_close_count"] = len(closes)

    try:
        prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=True)
        missing_bars = [c for c in closes if c not in prices]
        report["checks"]["ohlc"] = {
            "ok": len(missing_bars) == 0,
            "bar_count": len(prices),
            "missing_for_decision_closes": missing_bars[:5],
            "missing_count": len(missing_bars),
        }
        if missing_bars:
            report["blockers"].append("OHLC_INCOMPLETE")
    except Exception as exc:  # noqa: BLE001
        report["checks"]["ohlc"] = {"ok": False, "error": str(exc)}
        report["blockers"].append("OHLC_LOAD_FAILED")

    rcfg = ResearchDensityRunV1.from_config_path().config
    report["checks"]["density_warmup_hours"] = rcfg.warmup_hours
    if rcfg.warmup_hours < 168:
        report["blockers"].append("WARMUP_BELOW_168H")

    bundle_path = (cfg.get("density") or {}).get("validated_cache", {}).get("bundle_path")
    if bundle_path:
        bpath = Path(bundle_path)
        if not bpath.is_absolute():
            bpath = ROOT / bundle_path
        if bpath.is_dir():
            manifest = json.loads((bpath / "bundle_manifest.json").read_text(encoding="utf-8"))
            have = {e["decision_close_utc"] for e in manifest.get("entries") or []}
            missing_cache = [c for c in closes if c not in have]
            report["checks"]["cache_coverage"] = {
                "ok": len(missing_cache) == 0,
                "have": len(have),
                "need": len(closes),
                "missing_count": len(missing_cache),
            }
            if require_complete_cache and missing_cache:
                report["blockers"].append("CACHE_INCOMPLETE")
        else:
            report["checks"]["cache_coverage"] = {"ok": False, "note": "bundle not built yet"}
            if require_complete_cache:
                report["blockers"].append("CACHE_BUNDLE_MISSING")
    else:
        report["checks"]["cache_coverage"] = {"ok": False, "note": "no bundle_path"}
        if require_complete_cache:
            report["blockers"].append("CACHE_PATH_MISSING")

    if report["blockers"]:
        report["verdict"] = "BT_D9_24H_PNL_BLOCKED"
    return report
