"""Join Frozen signal rows with precomputed V1 artifacts. No planner calls."""

from __future__ import annotations

from typing import Any

from .config import STRATEGY_ID, enable_pool_order_plan_v1
from .schema import REASON_ARTIFACT, REASON_BAD_SOURCE, STATUS_NO_PLAN, is_clickhouse_candle_source
from .store import artifact_available, load_latest_index, load_latest_manifest


def overlay_enabled() -> bool:
    """Legacy helper: production latest overlay is unused by the dashboard API."""
    return enable_pool_order_plan_v1() and artifact_available()


def overlay_rows(baseline_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not is_clickhouse_candle_source(load_latest_manifest()):
        out = []
        for row in baseline_rows:
            item = dict(row)
            item["strategy_version"] = STRATEGY_ID
            item["research_backtest"] = True
            item["plan_status"] = STATUS_NO_PLAN
            item["no_plan_reason"] = REASON_BAD_SOURCE
            item["tp1_price"] = None
            item["tp2_price"] = None
            item["pool_sl_price"] = None
            out.append(item)
        return out
    index = load_latest_index()
    out = []
    for row in baseline_rows:
        item = dict(row)
        sid = str(item.get("signal_id") or "")
        art = index.get(sid) if sid else None
        item["strategy_version"] = STRATEGY_ID
        item["research_backtest"] = True
        if not art:
            item["plan_status"] = STATUS_NO_PLAN
            item["no_plan_reason"] = REASON_ARTIFACT
            item["tp1_price"] = None
            item["tp2_price"] = None
            item["pool_sl_price"] = None
            out.append(item)
            continue
        item["plan_status"] = art.get("plan_status")
        item["no_plan_reason"] = art.get("no_plan_reason")
        item["initial_target_mode"] = art.get("initial_target_mode")
        item["sl_price"] = art.get("sl_price")
        item["pool_sl_price"] = art.get("sl_price")
        item["sl_distance_pct"] = art.get("sl_distance_pct")
        item["sl_too_wide"] = art.get("sl_too_wide")
        item["tp1_price"] = art.get("tp1_price")
        item["tp1_size"] = art.get("tp1_size")
        item["tp2_price"] = art.get("tp2_price")
        item["tp2_size"] = art.get("tp2_size")
        item["tp_price"] = art.get("tp1_price")
        item["expected_tp"] = art.get("tp1_price")
        item["expected_sl"] = art.get("sl_price")
        item["result"] = art.get("outcome") or item.get("result")
        item["display_result"] = art.get("outcome") or item.get("display_result")
        item["pnl_pct"] = art.get("net_pnl_pct")
        item["gross_pnl_pct"] = art.get("gross_pnl_pct")
        item["fees_pct"] = art.get("fees_pct")
        item["net_pnl_pct"] = art.get("net_pnl_pct")
        item["baseline_tp_price"] = row.get("tp_price")
        item["baseline_sl_price"] = row.get("sl_price")
        out.append(item)
    return out
