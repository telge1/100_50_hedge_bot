#!/usr/bin/env python3
"""Thin launcher for research/volatility_event_detector (single-symbol smoke)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from volatility_event_detector.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
