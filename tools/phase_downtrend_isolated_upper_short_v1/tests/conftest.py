from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
ROOT = PKG.parents[1]
for p in (
    str(PKG),
    str(ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"),
    str(ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"),
    str(ROOT / "tools/phase4d_pool_landscape_backtester_v2"),
    str(ROOT / "tools/phase_bt_d46_integrated_short_backtester_v1"),
):
    if p not in sys.path:
        sys.path.insert(0, p)
