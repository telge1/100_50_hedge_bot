#!/usr/bin/env python3
"""Launcher: read-only multi-source data inventory audit V1."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.multisource_data_inventory_v1.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
