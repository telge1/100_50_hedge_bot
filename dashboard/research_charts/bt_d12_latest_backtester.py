"""BT-D12 — load pools+ob results/backtester_runs/latest_result.json (read-only)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

STRATEGY_ID = "pool_landscape_v1_candidate_bt_d7"
BACKTESTER_SOURCE = "bt_d12_latest_result_v1"

def resolve_pools_ob_repo() -> Path:
    env = os.environ.get("POOLS_OB_DELTA_BOT_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    sibling = here.parents[3] / "pools+ob+delta_bot"
    if sibling.is_dir():
        return sibling.resolve()
    embedded = here.parents[4]
    if (embedded / "results").is_dir() and (embedded / "tools").is_dir():
        return embedded.resolve()
    raise FileNotFoundError("pools+ob+delta_bot root not found")


def latest_result_path() -> Path:
    return resolve_pools_ob_repo() / "results" / "backtester_runs" / "latest_result.json"


def _ensure_bt_d10() -> None:
    p = str(resolve_pools_ob_repo() / "tools/phase_bt_d10_existing_viewer_overlay_integration_v1")
    if p not in sys.path:
        sys.path.insert(0, p)


def load_latest_result(*, symbol: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    sym = str(symbol or "").strip().upper()
    if sym != "DOGEUSDT":
        return None, None, None, ["symbol_not_allowed"]
    path = latest_result_path()
    if not path.is_file():
        return None, None, None, ["latest_result_missing"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None, None, ["latest_result_corrupt"]
    if str(doc.get("symbol") or "").upper() != sym:
        return None, None, None, ["symbol_mismatch"]
    integration = doc.get("integration") or {
        "manifest": doc.get("manifest") or {},
        "config_hash": doc.get("config_hash"),
        "signals": doc.get("signals"),
        "orders": doc.get("orders"),
        "trades": doc.get("trades"),
        "pnl": doc.get("pnl"),
    }
    _ensure_bt_d10()
    from overlay_export_v1 import export_overlay_bundle  # noqa: WPS433

    from .bt_d7_short_research_backtester import build_summary

    bundle = export_overlay_bundle(integration, integration_path=path, symbol=sym)
    bundle["run_id"] = str(doc.get("run_id") or "latest")
    summary = build_summary(integration, bundle, path)
    summary["run_id"] = doc.get("run_id")
    summary["source"] = BACKTESTER_SOURCE
    summary["latest_result_path"] = str(path)
    pnl = doc.get("pnl") or {}
    summary["net_pnl_usdt"] = pnl.get("total_net_pnl_usdt", pnl.get("net_pnl_usdt"))
    summary["return_pct"] = pnl.get("total_return_pct")
    summary["win_rate"] = pnl.get("win_rate_pct", pnl.get("win_rate"))
    times = [doc.get("start_utc"), doc.get("end_utc")]
    summary["time_span"] = {"start": times[0], "end": times[1]}
    if int(summary.get("filled_trade_count") or 0) == 0:
        summary["zero_trades_note"] = "0 Trades — keine realisierten Handelsgewinne (kein Strategienachweis)"
    return doc, bundle, summary, []
