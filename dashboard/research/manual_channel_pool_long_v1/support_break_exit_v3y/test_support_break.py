#!/usr/bin/env python3
"""Unit checks for V3Y support break (reference trade)."""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

V3Y = Path(__file__).resolve().parent
PROBE = V3Y.parent
if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))
if str(V3Y) not in sys.path:
    sys.path.insert(0, str(V3Y))

from v3y_config import REFERENCE_TRADE

import run_l1_pilot as l1
from line_geometry import fmt_utc
from context import build_frozen_context
from support_sim import simulate_exit_v3y


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def test_reference_august_long() -> None:
    frozen = build_frozen_context()
    candles = frozen["candles"]
    lines = frozen["lines_by_id"]
    sid = REFERENCE_TRADE["support_id"]
    assert sid in lines
    support = lines[sid]
    entry_ts = int(_parse(REFERENCE_TRADE["entry_utc"]).timestamp())
    entry_idx = next(i for i, c in enumerate(candles) if int(c["time"]) == entry_ts)
    entry = REFERENCE_TRADE["entry_price"]
    sl = entry * (1.0 - 0.012)
    tp = 0.09496
    kind, exit_ts, exit_px, pnl, exit_idx = simulate_exit_v3y(
        candles, entry_idx, entry, sl, tp, None, support
    )
    assert kind == "SUPPORT_BREAK_EXIT", kind
    assert fmt_utc(exit_ts) == REFERENCE_TRADE["exit_utc"], fmt_utc(exit_ts)
    assert abs(float(exit_px) - REFERENCE_TRADE["exit_price"]) < 1e-6
    assert abs(pnl - REFERENCE_TRADE["pnl_pct"]) < 0.05


def test_price_path_csv_exists() -> None:
    p = PROBE / "l1_v3v_price_path.csv"
    assert p.is_file()


if __name__ == "__main__":
    test_price_path_csv_exists()
    test_reference_august_long()
    print("OK")
