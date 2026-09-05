#!/usr/bin/env python3
"""Launcher: BTC/DOGE current multi-source recheck V1."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.btc_doge_current_recheck_v1.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
