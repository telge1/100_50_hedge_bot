#!/usr/bin/env python3
"""Wrapper for BTC OB Fight fact CLI (Phase 0–1)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.btc_ob_fight.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
