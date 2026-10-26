from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BT_D8 = Path(__file__).resolve().parents[1]
for p in (str(BT_D8), str(ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1")):
    if p not in sys.path:
        sys.path.insert(0, p)
