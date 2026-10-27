import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
ROOT = PKG.parents[1]
for p in (
    str(PKG),
    str(ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"),
    str(ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1"),
    str(ROOT / "tools/phase4d_pool_landscape_backtester_v2"),
):
    if p not in sys.path:
        sys.path.insert(0, p)
