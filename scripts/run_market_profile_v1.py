#!/usr/bin/env python3
"""Isolated market-profile research runner.

Read-only ClickHouse queries, PNG/JSON/CSV/Markdown output into
``results/market_profile_v1/``. Touches no live collector, strategy, freeze or
dashboard path.

Examples::

    # one profile per UTC day
    python scripts/run_market_profile_v1.py --symbol BTCUSDT \
        --start 2026-08-24T00:00:00Z --end 2026-08-31T00:00:00Z --anchor day

    # per liquidity session (US = NYSE cash-session analogue)
    python scripts/run_market_profile_v1.py --symbol BTCUSDT \
        --start 2026-08-28T00:00:00Z --end 2026-08-31T00:00:00Z \
        --anchor session --sessions us,eu --timeframe 5m

    # one merged profile over a balance period
    python scripts/run_market_profile_v1.py --symbol BTCUSDT \
        --start 2026-08-20T00:00:00Z --end 2026-08-31T00:00:00Z \
        --anchor composite --timeframe 1h
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.market_profile.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
