from __future__ import annotations

import json
import sys
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[2]
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

SG = Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src")
if str(SG) not in sys.path:
    sys.path.insert(0, str(SG))

PLANNER = Path("/home/telgenbuescher/projects/pool_order_planer")
if str(PLANNER) not in sys.path:
    sys.path.insert(0, str(PLANNER))
