#!/usr/bin/env python3
"""Regenerate short_pool_oracle_v1_golden.json (read-only CH). Not run in CI by default."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parents[5]
_dash = _repo / "backtester" / "short"
sys.path.insert(0, str(_repo))
sys.path.insert(0, str(_dash))

from pool_pattern.market import ensure_paths

ensure_paths()

from dashboard.research_charts.short_pool_oracle_v1 import (
    ORACLE_FIXTURES,
    short_pool_oracle_snapshot,
)

OUT = Path(__file__).resolve().parent / "short_pool_oracle_v1_golden.json"


def main() -> int:
    golden: dict = {}
    for symbol, decision_iso in ORACLE_FIXTURES:
        key = f"{symbol}|{decision_iso}"
        snap = short_pool_oracle_snapshot(symbol, decision_iso)
        golden[key] = {
            "symbol": snap["symbol"],
            "decision_timestamp": snap["decision_timestamp"],
            "entry_open": snap["entry_open"],
            "candle_index": snap["candle_index"],
            "pane_first_timestamp": snap["pane_first_timestamp"],
            "pane_last_timestamp": snap["pane_last_timestamp"],
            "lld_config": snap["lld_config"],
            "available_pool_ids": snap["available_pool_ids"],
            "available_count": snap["available_count"],
            "available_rows_hash": snap["available_rows_hash"],
        }
    OUT.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(golden)} fixtures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
