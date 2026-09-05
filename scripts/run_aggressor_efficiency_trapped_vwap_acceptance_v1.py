#!/usr/bin/env python3
"""Run AGGRESSOR_EFFICIENCY_TRAPPED_VWAP_ACCEPTANCE_V1 research smoke."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
