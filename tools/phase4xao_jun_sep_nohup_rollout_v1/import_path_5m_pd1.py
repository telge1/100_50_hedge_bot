"""Force spread-dashboard research_charts for 5m PD1 historical readback (avoids pools 15m-only shadowing)."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester/short/dashboard"
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")


def ensure_5m_pd1_readback_import_path() -> dict[str, str]:
    """
    Pools-dashboard ``historical_pool_state_v1`` calls ``resolve_historical_pd_config(pd)``
    without timeframe (15m only). Spread dashboard passes ``timeframe=`` and supports 5m PD1.
    Month validation loads pools modules into ``sys.modules``; purge before readback.
    """
    dash = SPREAD_DASH if SPREAD_DASH.is_dir() else POOLS_DASH
    for mod in list(sys.modules):
        if mod.startswith("research_charts"):
            del sys.modules[mod]
    sys.path = [p for p in sys.path if p not in (str(POOLS_DASH), str(SPREAD_DASH))]
    for p in (ROOT, TRP, dash):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    from research_charts.historical_lmv2_pd_contract_v1 import (
        resolve_historical_pd_config,
        resolve_viewer_historical_scope,
    )
    from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close

    hcfg = resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)
    probe = resolve_historical_pd_config(1, timeframe="5m")
    contract_path = inspect.getfile(resolve_historical_pd_config)
    hps_path = inspect.getfile(load_historical_pool_state_at_close)
    return {
        "dashboard": str(dash),
        "contract_module": contract_path,
        "historical_pool_state_module": hps_path,
        "resolved_timeframe": hcfg.timeframe,
        "resolved_pipeline_line_id": hcfg.pipeline_line_id,
        "probe_5m_timeframe": probe.timeframe,
    }


def setup_hcfg() -> Any:
    ensure_5m_pd1_readback_import_path()
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope

    return resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)
