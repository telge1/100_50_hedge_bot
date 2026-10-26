import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parents[1]
for p in (
    str(TOOLS),
    str(ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"),
    str(ROOT / "tools/phase_bt_d4_enforced_depth2_backtester_v1"),
    str(ROOT / "tools/phase4d_pool_landscape_backtester_v2"),
):
    if p not in sys.path:
        sys.path.insert(0, p)
