#!/usr/bin/env python3
"""Launcher: BTC raw vs aggregate parity root-cause audit V1."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.btc_raw_aggregate_parity_audit_v1.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
