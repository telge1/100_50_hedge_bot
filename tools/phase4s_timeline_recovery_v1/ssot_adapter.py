"""Isolated SSOT / dashboard import path for Phase 4S (read-only)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester" / "short" / "dashboard"
TRP_ROOT = Path("/home/telgenbuescher/projects/trading_research_platform")
MAX_EXPORT_HOURS = 72


def _purge_dashboard_modules() -> None:
    for name in list(sys.modules):
        if name.startswith("research_charts") or name.startswith("pd1_landscape_viewer_v1"):
            del sys.modules[name]


def apply_ssot_pythonpath() -> dict[str, str]:
    """Prefer spread dashboard contract (ALLOWED_VIEWER_SCOPES) + trading_research_platform `data`."""
    dash = SPREAD_DASH if SPREAD_DASH.is_dir() else POOLS_DASH
    os.environ["PHASE1B_DASHBOARD_ROOT"] = str(dash)
    paths = []
    if TRP_ROOT.is_dir():
        paths.append(str(TRP_ROOT))
    paths.append(str(dash))
    # Remove pools dashboard shadow from path head if present.
    sys.path = [p for p in sys.path if p != str(POOLS_DASH)]
    _purge_dashboard_modules()
    for p in reversed(paths):
        if p not in sys.path:
            sys.path.insert(0, p)
    return {
        "PHASE1B_DASHBOARD_ROOT": os.environ["PHASE1B_DASHBOARD_ROOT"],
        "pythonpath_head": sys.path[:4],
    }


apply_ssot_pythonpath()


def import_error_traceback_probe() -> dict[str, Any]:
    """Reproduce pools-only vs adapter import failures."""
    pools_only_err = ""
    try:
        if str(POOLS_DASH) not in sys.path:
            sys.path.insert(0, str(POOLS_DASH))
        from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window  # noqa: F401
        pools_only_err = "OK"
    except Exception as exc:
        pools_only_err = f"{type(exc).__name__}: {exc}"

    apply_ssot_pythonpath()
    adapter_err = ""
    try:
        from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window  # noqa: F401

        adapter_err = "OK"
    except Exception as exc:
        import traceback

        adapter_err = traceback.format_exc()
    return {"pools_dashboard_only": pools_only_err, "phase4s_adapter": adapter_err}


def build_landscape_window(vis_from: str, vis_to: str) -> dict[str, Any]:
    apply_ssot_pythonpath()
    from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window as _blw

    return _blw(vis_from, vis_to, timeframe="5m", pool_detail=1)


def published_inventory() -> dict[str, Any]:
    apply_ssot_pythonpath()
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope
    from research_charts.historical_pool_state_v1 import (
        get_published_decision_closes,
        static_timeline_import_bounds,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    hcfg = resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)
    reader = PublishedActiveReader()
    pub = get_published_decision_closes(
        reader, hcfg.config_hash, pipeline_line_id=hcfg.pipeline_line_id
    )
    lo, hi = static_timeline_import_bounds(pub)
    json_root = (
        POOLS_DASH
        / "research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history/production/results/2026-09/5m_PD1"
    )
    json_count = len(list(json_root.glob("*.json"))) if json_root.is_dir() else 0
    return {
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "pool_detail": 1,
        "pipeline_line_id": hcfg.pipeline_line_id,
        "config_hash": hcfg.config_hash,
        "published_from_utc": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_to_utc": hi.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_decision_closes": len(pub),
        "frozen_json_files_sep_dir": json_count,
    }
