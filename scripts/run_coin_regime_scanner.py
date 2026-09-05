#!/usr/bin/env python3
"""Thin launcher for COIN_REGIME_SCANNER_V1 (research-only)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.coin_regime_scanner.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
