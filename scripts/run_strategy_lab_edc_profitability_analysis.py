#!/usr/bin/env python3
"""CLI entrypoint for P2E1 EDC profitability diagnosis.

No ClickHouse and no network. Inputs are local CSV/JSON artifacts only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/...` without requiring editable install when PYTHONPATH=src.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orderbook_analyse.strategy_lab.analysis.edc_profitability_v2 import main


if __name__ == "__main__":
    raise SystemExit(main())
