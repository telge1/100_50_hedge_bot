#!/usr/bin/env python3
"""Evaluate frozen simple liquidity-destination baselines (research only)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.liquidity_destination_bias.baselines.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
