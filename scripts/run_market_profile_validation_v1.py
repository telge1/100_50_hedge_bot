#!/usr/bin/env python3
"""Does the balance/trend classification predict anything?

Read-only. Builds each reference window's profile from that window's trades
only, then scores what price does in the following window. Writes JSON/CSV/
Markdown into ``results/market_profile_validation_v1/``.

Examples::

    # two symbols, quick check
    python scripts/run_market_profile_validation_v1.py \
        --symbols BTCUSDT,ETHUSDT \
        --start 2026-07-20T00:00:00Z --end 2026-08-31T00:00:00Z

    # full frozen universe
    python scripts/run_market_profile_validation_v1.py --symbols universe \
        --start 2026-07-20T00:00:00Z --end 2026-08-31T00:00:00Z
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.market_profile_validation.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
