from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OA_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if OA_SRC.is_dir() and str(OA_SRC) not in sys.path:
    sys.path.insert(0, str(OA_SRC))
